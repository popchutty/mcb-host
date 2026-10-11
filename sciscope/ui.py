from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W
from serial.tools import list_ports

from . import __version__
from .acquisition import AcquisitionWorker, DataHub
from .analysis import amplitude_spectrum, peak_trace, statistics
from .config import Channel, Profile, TYPES
from .display import DEFAULT_BUFFER_POINTS, MAX_BUFFER_POINTS, ScopeSettings
from .motor import DEBUG_PAIRS, FocSettings, mapped_profile, motor_packet, official_profile
from .dyno import DYNO_DEBUG_PAIRS, DynoSettings, MotorBases, dyno_mapped_profile, dyno_packet, dyno_profile
from .protocol import encode_values, marker_bytes, physical_values
from .storage import SegmentReader, export_segment, session_files
from .matlab_viewer import open_recording as open_matlab_recording

pg.setConfigOptions(antialias=False, background="#101b2d", foreground="#ced8ea")
COLORS = ["#53d7c4", "#f4b75e", "#6d9eff", "#ef8baf", "#bc9cff", "#9dde73", "#e18a66"]


def line(text=""):
    return W.QLineEdit(str(text))


def combo(values, current=None):
    result = W.QComboBox()
    result.addItems(values)
    if current is not None:
        result.setCurrentText(str(current))
    return result


def integer(value, low=1, high=1000000):
    result = W.QSpinBox()
    result.setRange(low, high)
    result.setValue(value)
    return result


class NumericInput(W.QDoubleSpinBox):
    display_decimals = None

    def textFromValue(self, value):
        precision = self.decimals() if self.display_decimals is None else self.display_decimals
        return f"{value:.{precision}f}".rstrip("0").rstrip(".") or "0"


def decimal(value, low=0.000001, high=1e9, *, decimals=6, display_decimals=None):
    result = NumericInput()
    result.display_decimals = display_decimals
    result.setDecimals(decimals)
    result.setRange(low, high)
    result.setValue(value)
    return result


def button(text, callback):
    result = W.QPushButton(text)
    result.clicked.connect(callback)
    return result


def table(headers):
    result = W.QTableWidget(0, len(headers))
    result.setHorizontalHeaderLabels(headers)
    result.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Stretch)
    result.setAlternatingRowColors(True)
    return result


def cell(widget, row, col, default=""):
    item = widget.item(row, col)
    return item.text() if item is not None else default


def fill_row(widget, row, values):
    for col, value in enumerate(values):
        widget.setItem(row, col, W.QTableWidgetItem(str(value)))


class SidebarHandle(W.QSplitterHandle):
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setCursor(QtCore.Qt.CursorShape.SplitHCursor)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("#dbe7f6" if self.underMouse() else "#edf1f7"))
        painter.setPen(pg.mkPen("#8c9bb1", width=2))
        middle = self.rect().center()
        for offset in (-12, -6, 0, 6, 12):
            painter.drawPoint(middle.x(), middle.y() + offset)


class SidebarSplitter(W.QSplitter):
    def __init__(self):
        super().__init__(QtCore.Qt.Orientation.Horizontal)
        self.setHandleWidth(10)
        self.setChildrenCollapsible(False)

    def createHandle(self):
        return SidebarHandle(self.orientation(), self)


class ScopeToolbar(W.QScrollArea):
    def __init__(self, row):
        super().__init__()
        self.setFrameShape(W.QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(W.QSizePolicy.Policy.Expanding, W.QSizePolicy.Policy.Fixed)
        self.setWidget(row)
        row.setAutoFillBackground(True)
        row.setBackgroundRole(QtGui.QPalette.ColorRole.Base)
        self.viewport().setAutoFillBackground(True)
        self.viewport().setBackgroundRole(QtGui.QPalette.ColorRole.Base)
        row.installEventFilter(self)
        self.update_height()

    def update_height(self):
        row = self.widget()
        if row is None:
            return
        extra = self.horizontalScrollBar().sizeHint().height() if row.minimumSizeHint().width() > self.width() else 0
        height = row.sizeHint().height() + extra
        if height != self.height():
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_height()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.LayoutRequest:
            self.update_height()
        return super().eventFilter(watched, event)


class ScopeWidget(W.QWidget):
    def __init__(self, parent=None, *, live=True):
        super().__init__(parent)
        self.live = live
        self.graphics = pg.GraphicsLayoutWidget()
        self.graphics.scene().setItemIndexMethod(W.QGraphicsScene.ItemIndexMethod.NoIndex)
        self.graphics.setMinimumWidth(200)
        self.channels = W.QListWidget()
        self.channel_width = 190
        self.channels_collapsed = False
        self.channel_pane = W.QWidget()
        self.channel_pane.setMinimumWidth(100)
        channel_layout = W.QVBoxLayout(self.channel_pane)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        channel_layout.setSpacing(4)
        channel_header = W.QHBoxLayout()
        channel_header.addWidget(W.QLabel("信号"))
        channel_header.addStretch()
        self.collapse_channels = W.QToolButton()
        self.collapse_channels.setArrowType(QtCore.Qt.ArrowType.LeftArrow)
        self.collapse_channels.setToolTip("折叠信号栏")
        self.collapse_channels.setFixedSize(24, 24)
        self.collapse_channels.clicked.connect(lambda: self.set_channels_collapsed(True))
        channel_header.addWidget(self.collapse_channels)
        channel_layout.addLayout(channel_header)
        channel_layout.addWidget(self.channels, 1)
        self.channel_splitter = SidebarSplitter()
        self.channel_splitter.addWidget(self.channel_pane)
        self.channel_splitter.addWidget(self.graphics)
        self.channel_splitter.setStretchFactor(0, 0)
        self.channel_splitter.setStretchFactor(1, 1)
        self.channel_splitter.setSizes([self.channel_width, 1000])
        self.channel_splitter.splitterMoved.connect(self.remember_channel_width)
        self.channel_rail = W.QWidget()
        self.channel_rail.setFixedWidth(24)
        rail_layout = W.QVBoxLayout(self.channel_rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        self.expand_channels = W.QToolButton()
        self.expand_channels.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.expand_channels.setToolTip("展开信号栏")
        self.expand_channels.setFixedSize(24, 24)
        self.expand_channels.clicked.connect(lambda: self.set_channels_collapsed(False))
        rail_layout.addWidget(self.expand_channels)
        rail_layout.addStretch()
        self.channel_rail.hide()
        self.mode = combo(["叠加", "分图"])
        self.mode.setMaximumWidth(70)
        self.paused = W.QCheckBox("暂停显示")
        self.window = decimal(1, 0.01, 30)
        self.window.setSuffix(" s")
        self.window.setMinimumWidth(64)
        self.window.setMaximumWidth(100)
        self.units = W.QCheckBox("通道换算")
        self.units.setChecked(True)
        self.cursor_enabled = W.QCheckBox("游标")
        if live:
            self.buffer_points = integer(DEFAULT_BUFFER_POINTS, 1, MAX_BUFFER_POINTS)
            self.buffer_points.setGroupSeparatorShown(True)
            self.buffer_points.setKeyboardTracking(False)
            self.buffer_points.setSingleStep(1000)
            self.buffer_points.setMinimumWidth(85)
            self.buffer_points.setMaximumWidth(120)
        self.readout = W.QLabel("等待数据")
        self.readout.setWordWrap(True)
        self.cache_readout = W.QLabel()
        self.cache_readout.hide()
        self.cursor_label = W.QLabel()
        self.cursor_label.hide()
        self.controls_row = W.QWidget()
        tools = W.QHBoxLayout(self.controls_row)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        controls = [W.QLabel("显示"), self.mode]
        if live:
            controls += [W.QLabel("时间窗口"), self.window, W.QLabel("存储深度（点/通道）"), self.buffer_points]
        else:
            for widget in (self.window, self.paused):
                widget.setParent(self)
                widget.hide()
        self.autorange_button = button("自动量程", self.autorange)
        for widget in controls + [self.autorange_button] + ([self.paused] if live else []) + [self.units, self.cursor_enabled]:
            tools.addWidget(widget)
        self.units.hide()
        tools.addStretch()
        self.controls_scroll = ScopeToolbar(self.controls_row)
        content = W.QHBoxLayout()
        content.setSpacing(4)
        content.addWidget(self.channel_rail)
        content.addWidget(self.channel_splitter, 1)
        layout = W.QVBoxLayout(self)
        layout.addWidget(self.controls_scroll)
        layout.addLayout(content, 1)
        layout.addWidget(self.readout)
        layout.addWidget(self.cache_readout)
        layout.addWidget(self.cursor_label)
        self.curves = {}
        self.plots = []
        self.profile = None
        self.last_t = np.empty(0)
        self.last_data = np.empty((0, 0))
        self.last_valid = None
        self.capture_key = None
        self._data_revision = 0
        self._draw_key = None
        self._has_view = False
        self.render_timer = QtCore.QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self.render)
        self.mode.currentTextChanged.connect(self.rebuild)
        self.channels.itemChanged.connect(self.rebuild)
        self.window.valueChanged.connect(self.window_changed)
        self.paused.toggled.connect(self.pause_indicator)
        self.cursor_enabled.toggled.connect(self.toggle_cursors)

    def remember_channel_width(self, *_):
        if not self.channels_collapsed:
            self.channel_width = self.channel_pane.width()

    def restore_channel_width(self):
        if not self.channels_collapsed:
            available = self.channel_splitter.width() - self.channel_splitter.handleWidth()
            self.channel_splitter.setSizes([self.channel_width, max(0, available - self.channel_width)])

    def set_channels_collapsed(self, collapsed):
        if collapsed == self.channels_collapsed:
            return
        if collapsed and self.channel_pane.isVisible():
            self.channel_width = self.channel_pane.width()
        self.channels_collapsed = collapsed
        self.channel_pane.setVisible(not collapsed)
        self.channel_rail.setVisible(collapsed)
        if not collapsed:
            QtCore.QTimer.singleShot(0, self.restore_channel_width)

    def showEvent(self, event):
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self.restore_channel_width)

    def configure(self, profile: Profile):
        self.profile = profile.clone()
        self.capture_key = None
        self.last_t = np.empty(0)
        self.last_data = np.empty((0, profile.rx.channels))
        self.last_valid = None
        self.update_buffer_settings()
        self._has_view = False
        self.readout.setText("等待数据")
        self.cursor_label.clear()
        self.channels.blockSignals(True)
        self.channels.clear()
        for index, c in enumerate(profile.rx.channel_info):
            item = W.QListWidgetItem(c.name + (f" [{c.unit}]" if c.unit else ""))
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if profile.rx.channels <= 14 or index < 8 else QtCore.Qt.CheckState.Unchecked)
            self.channels.addItem(item)
        self.channels.blockSignals(False)
        self.rebuild()

    def update_buffer_settings(self):
        if not self.live or self.profile is None:
            return
        blocker = QtCore.QSignalBlocker(self.buffer_points)
        self.buffer_points.setRange(1, ScopeSettings.point_limit(self.profile))
        del blocker
        _, size, seconds = ScopeSettings(self.buffer_points.value()).plan(self.profile)
        self.buffer_points.setToolTip(f"{self.buffer_points.value():,} 点/通道\n缓存时长 {seconds:.3f} s\n原始缓存 {size / 1024**2:.2f} MiB")
        frozen_seconds = len(self.last_t) / self.profile.rx.sample_rate if self.paused.isChecked() else 0
        self.window.setRange(min(0.01, seconds), max(seconds, frozen_seconds))

    def selected(self):
        return [i for i in range(self.channels.count()) if self.channels.item(i).checkState() == QtCore.Qt.CheckState.Checked]

    def clear_plots(self):
        self.render_timer.stop()
        if self.plots:
            self.plots[0].vb.sigXRangeChanged.disconnect(self.request_render)
            for plot in self.plots:
                plot.vb.sigResized.disconnect(self.request_render)
                plot.setXLink(None)
            for plot in self.plots:
                plot.clear()
        self.graphics.clear()
        self.curves.clear()
        self.plots.clear()

    def closeEvent(self, event):
        self.clear_plots()
        super().closeEvent(event)

    def rebuild(self, *_):
        if self.profile is None:
            return
        limits = self.plots[0].viewRange()[0] if self.plots else None
        cursors = (self.cursor_a.value(), self.cursor_b.value()) if self.plots else (0, 0.05)
        self._draw_key = None
        self.clear_plots()
        selected = self.selected()
        if not selected:
            self.readout.clear()
            self.cursor_label.clear()
            self.cursor_label.hide()
            return
        for position, index in enumerate(selected):
            if self.mode.currentText() == "分图" or position == 0:
                plot = self.graphics.addPlot(row=len(self.plots), col=0)
                plot.showGrid(x=True, y=True, alpha=0.15)
                plot.setLabel("bottom", "时间", units="s")
                plot.addLegend()
                plot.disableAutoRange(axis="x")
                if limits:
                    plot.setXRange(*limits, padding=0)
                plot.vb.sigResized.connect(self.request_render)
                if self.plots:
                    plot.setXLink(self.plots[0])
                self.plots.append(plot)
            plot = self.plots[-1]
            c = self.profile.rx.channel_info[index]
            curve = plot.plot(pen=pg.mkPen(COLORS[index % len(COLORS)], width=1), name=c.name + (f" [{c.unit}]" if c.unit else ""))
            curve.setDownsampling(ds=1, auto=False)
            self.curves[index] = curve
        self.plots[0].vb.sigXRangeChanged.connect(self.request_render)
        self.cursor_a = pg.InfiniteLine(pos=cursors[0], movable=True, pen="#bac4d4")
        self.cursor_b = pg.InfiniteLine(pos=cursors[1], movable=True, pen="#f4b75e")
        for cursor in (self.cursor_a, self.cursor_b):
            if self.cursor_enabled.isChecked():
                self.plots[0].addItem(cursor, ignoreBounds=True)
            cursor.setVisible(self.cursor_enabled.isChecked())
            cursor.sigPositionChanged.connect(self.cursor_readout)
        self.render()
        self.update_readout()

    def cursor_readout(self):
        if not self.cursor_enabled.isChecked():
            self.cursor_label.clear()
            self.cursor_label.hide()
            return
        if not self.plots:
            return
        self.cursor_label.show()
        a, b = self.cursor_a.value(), self.cursor_b.value()
        message = f"游标 A={a:.6g}s，B={b:.6g}s，Δt={b - a:.6g}s"
        if len(self.last_t) and self.curves:
            i = next(iter(self.curves))
            ia = min(len(self.last_t) - 1, max(0, int(np.searchsorted(self.last_t, a))))
            ib = min(len(self.last_t) - 1, max(0, int(np.searchsorted(self.last_t, b))))
            available = self.last_t[0] <= min(a, b) and max(a, b) <= self.last_t[-1]
            values = self.last_data[[ia, ib], i]
            if self.last_valid is not None:
                available = available and self.last_valid[[ia, ib]].all()
            delta = f"{values[1] - values[0]:.6g}" if available and np.isfinite(values).all() else "—"
            message += f"；{self.profile.rx.channel_info[i].name} Δy={delta}"
        self.cursor_label.setText(message)

    def toggle_cursors(self, enabled):
        if self.plots:
            begin, end = self.plots[0].viewRange()[0]
            for cursor, fraction in ((self.cursor_a, 0.25), (self.cursor_b, 0.75)):
                if enabled and not begin <= cursor.value() <= end:
                    cursor.setValue(begin + (end - begin) * fraction)
                if enabled and cursor.scene() is None:
                    self.plots[0].addItem(cursor, ignoreBounds=True)
                elif not enabled:
                    self.plots[0].removeItem(cursor)
                cursor.setVisible(enabled)
        self.cursor_label.setVisible(enabled and bool(self.plots))
        self.cursor_readout()

    def autorange(self):
        for plot in self.plots:
            plot.enableAutoRange(axis="y")

    def update_data(self, t: np.ndarray, data: np.ndarray, valid=None):
        if self.paused.isChecked():
            return
        self.last_t, self.last_data, self.last_valid = t, data, valid
        self._data_revision += 1
        if len(t) and self.plots:
            if self.live:
                self.set_latest_range()
            elif not self._has_view:
                self.plots[0].setXRange(t[0], max(t[-1], t[0] + 1e-6), padding=0)
            self._has_view = True
        self.render()
        if not len(t):
            self.readout.setText("该区间无缓存数据")
            self.cursor_readout()
            return
        self.update_readout()

    def update_readout(self):
        if len(self.last_t):
            self.readout.setText("   |   ".join(f"{self.profile.rx.channel_info[i].name}: {self.last_data[-1, i]:.6g}" for i in self.curves))
        self.cursor_readout()
        self.pause_indicator(self.paused.isChecked())

    def pause_indicator(self, paused):
        self.cache_readout.setVisible(self.live and paused)
        if len(self.last_t):
            self.cache_readout.setText(f"可查看 {self.last_t[0]:.3f}–{self.last_t[-1]:.3f} s · {len(self.last_t):,} 点/通道")
        else:
            self.cache_readout.setText("暂无缓存数据")

    def freeze_data(self, t, data, valid):
        if not len(t):
            return
        self.last_t, self.last_data, self.last_valid = t, data, valid
        self.update_buffer_settings()
        self._data_revision += 1
        self.render()
        self.update_readout()

    def set_latest_range(self):
        if not len(self.last_t) or not self.plots:
            return
        end = self.last_t[-1]
        begin = max(self.last_t[0], end - self.window.value())
        self.plots[0].setXRange(begin, max(end, begin + 1e-6), padding=0)
        for cursor, fraction in ((self.cursor_a, 0.25), (self.cursor_b, 0.75)):
            if cursor.value() < begin or cursor.value() > end:
                cursor.setValue(begin + (end - begin) * fraction)

    def window_changed(self, *_):
        self.capture_key = None
        if self.live:
            self.set_latest_range()

    def capture_rows(self, sample_rate, total):
        count = int(np.ceil(self.window.value() * sample_rate)) + 2
        return max(0, total - count), total

    def request_render(self, *_):
        if not self.render_timer.isActive():
            self.render_timer.start(0)

    def render(self):
        if not self.plots:
            return
        begin, end = self.plots[0].viewRange()[0]
        low = max(0, int(np.searchsorted(self.last_t, begin)) - 1)
        high = min(len(self.last_t), int(np.searchsorted(self.last_t, end, side="right")) + 1)
        # Bound total geometry as channel count grows, while retaining every bin's peaks.
        budget = max(256, 8000 // max(1, len(self.curves)))
        bins = max(64, min(2048, budget, int(self.plots[0].vb.width())))
        key = (self._data_revision, low, high, bins)
        if key == self._draw_key:
            return
        self._draw_key = key
        t = self.last_t[low:high]
        for i, curve in self.curves.items():
            values = self.last_data[low:high, i]
            if self.last_valid is not None and not self.last_valid[low:high].all():
                values = values.copy()
                values[~self.last_valid[low:high]] = np.nan
            draw_t, draw_values = peak_trace(t, values, bins)
            curve.setData(draw_t, draw_values, connect="finite")


class JobWorker(QtCore.QThread):
    result = QtCore.Signal(object)
    error = QtCore.Signal(str)
    progress = QtCore.Signal(int)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancelled = False

    def report(self, fraction):
        if self.cancelled:
            raise InterruptedError("任务已取消")
        self.progress.emit(int(fraction * 100))

    def run(self):
        try:
            self.result.emit(self.function(self.report))
        except Exception as exc:
            self.error.emit(str(exc))


class FocControlPanel(W.QWidget):
    """Explicit, optional model adapter; no interception of generic TX values."""
    origin_prefix = "foc"
    required_length = 2
    packet = staticmethod(motor_packet)
    mapping = staticmethod(mapped_profile)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.enabled = False
        self.loading = True
        self.n_base, self.i_base = decimal(4107), decimal(21.428571428571427)
        self.base_widgets = {"motor1": {"n_base": self.n_base, "i_base": self.i_base}}
        self.rpm = decimal(2000, -100000, 100000)
        self.rpm.setSuffix(" RPM")
        self.selection = combo([f"{i + 1}: {a} / {b}" for i, (a, b, _, _) in enumerate(DEBUG_PAIRS)])
        self.map_channels = W.QCheckBox("联动接收通道")
        self.build_controls(owner.use_foc_preset,
                            [("n_base", "N_base (RPM)"), ("i_base", "I_base (A)")],
                            [("给定转速", self.rpm)], "启动电机", "停止电机")

    def build_controls(self, preset_loader, base_labels, control_rows, start_label, stop_label):
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.preset_row = W.QWidget()
        preset_layout = W.QHBoxLayout(self.preset_row)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        self.preset = combo(["single", "Q28"])
        preset_layout.addWidget(self.preset, 1)
        preset_layout.addWidget(button("使用预设", lambda: preset_loader(self.preset.currentIndex() == 1)))
        layout.addWidget(self.preset_row)
        self.parameter_group = W.QGroupBox("电机参数与控制")
        form = W.QVBoxLayout(self.parameter_group)
        self.base_grid = W.QWidget()
        grid = W.QGridLayout(self.base_grid)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(W.QLabel("基准值"), 0, 0)
        for column, motor in enumerate(self.base_widgets, 1):
            title = "电机" if len(self.base_widgets) == 1 else f"电机 {column}"
            grid.addWidget(W.QLabel(title), 0, column)
            grid.setColumnStretch(column, 1)
        for row, (key, label) in enumerate(base_labels, 1):
            grid.addWidget(W.QLabel(label), row, 0)
            for column, widgets in enumerate(self.base_widgets.values(), 1):
                widget = widgets[key]
                widget.setMinimumWidth(100)
                grid.addWidget(widget, row, column)
                widget.editingFinished.connect(self.save_settings)
        form.addWidget(self.base_grid)
        self.selection.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.selection.setMinimumContentsLength(12)
        for row, (label, widget) in enumerate(control_rows + [("调试信号组", self.selection),
                                                             ("接收映射", self.map_channels)], len(base_labels) + 1):
            grid.addWidget(W.QLabel(label), row, 0)
            grid.addWidget(widget, row, 1, 1, len(self.base_widgets))
        self.update_button = button("更新参数", lambda: self.send(self.enabled, origin=self.origin_prefix + ":update"))
        self.update_button.setMinimumWidth(120)
        update_row = W.QHBoxLayout()
        update_row.addStretch()
        update_row.addWidget(self.update_button)
        form.addLayout(update_row)
        layout.addWidget(self.parameter_group)
        row = W.QHBoxLayout()
        self.start_button = button(start_label, lambda: self.send(True))
        self.stop_button = button(stop_label, lambda: self.send(False))
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        self.message = W.QLabel("未发送命令")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.hex_preview = W.QPlainTextEdit()
        self.hex_preview.setReadOnly(True)
        self.hex_preview.setMaximumHeight(100)
        layout.addWidget(self.hex_preview)
        layout.addStretch()
        for _, widget in control_rows:
            widget.editingFinished.connect(self.save_settings)
        self.map_channels.toggled.connect(self.save_settings)
        self.selection.currentIndexChanged.connect(self.save_settings)
        self.load_profile(self.owner.profile)

    def settings(self):
        return FocSettings(self.n_base.value(), self.i_base.value(), self.rpm.value(),
                           self.selection.currentIndex() + 1, self.map_channels.isChecked())

    def load_profile(self, profile):
        self.loading = True
        error = ""
        try:
            s = FocSettings.from_profile(profile)
        except (TypeError, ValueError) as exc:
            s, error = FocSettings(), str(exc)
        self.preset.setCurrentIndex(1 if profile.tx.data_type == "uint32" else 0)
        self.n_base.setValue(s.n_base)
        self.i_base.setValue(s.i_base)
        self.rpm.setValue(s.rpm)
        self.selection.setCurrentIndex(s.selection - 1)
        self.map_channels.setChecked(s.map_channels)
        self.loading = False
        self.reset_connection()
        self.hex_preview.clear()
        if error:
            self.message.setText(error)
        self.update_buttons()

    def save_settings(self, *_):
        if not self.loading:
            self.settings().save_to(self.owner.profile)
            self.update_buttons()

    def compatible(self):
        p = self.owner.profile
        return (p.tx.data_length == self.required_length and p.tx.data_type in ("single", "uint32")
                and (not self.map_channels.isChecked() or (p.rx.channels == 2 and p.rx.data_type in ("single", "uint32"))))

    def update_buttons(self):
        connected = self.owner.worker is not None
        self.preset_row.setEnabled(not connected)
        for widget in (self.start_button, self.stop_button, self.update_button):
            widget.setEnabled(connected and self.compatible())

    def reset_connection(self):
        self.enabled = False
        self.message.setText("未发送命令")

    def send(self, enabled, *, origin=None):
        owner = self.owner
        try:
            if not owner.worker or not owner.worker.isRunning():
                raise ValueError("请先在主界面连接串口或模拟数据源")
            owner.periodic_check.setChecked(False)
            settings = self.settings()
            payload = self.packet(owner.profile, settings, enabled)
            metadata = None
            if settings.map_channels:
                candidate = self.mapping(owner.profile, settings)
                if candidate.rx.channel_info != owner.hub.profile.rx.channel_info:
                    metadata = candidate
            settings.save_to(owner.profile)
            owner.worker.send(payload, urgent=not enabled, metadata=metadata, transition_frames=2 if metadata else 0,
                              origin=origin or (self.origin_prefix + (":start" if enabled else ":stop")))
            self.enabled = enabled
            action = "更新" if origin == self.origin_prefix + ":update" else ("启动" if enabled else "停止")
            self.message.setText(action + "命令已排队")
            self.hex_preview.setPlainText(payload.hex(" ").upper())
        except Exception as exc:
            owner.notify(str(exc))


class DynoControlPanel(FocControlPanel):
    origin_prefix = "dyno"
    required_length = 4
    packet = staticmethod(dyno_packet)
    mapping = staticmethod(dyno_mapped_profile)

    def __init__(self, owner):
        W.QWidget.__init__(self, owner)
        self.owner = owner
        self.enabled = False
        self.loading = True
        self.rpm = decimal(1200, -100000, 100000)
        self.rpm.setSuffix(" RPM")
        self.imag_ref = decimal(1, -100000, 100000)
        self.imag_ref.setSuffix(" A")
        self.imag_pos = decimal(90, -360000, 360000)
        self.imag_pos.setSuffix(" °")
        self.selection = W.QComboBox()
        for index, pair in enumerate(DYNO_DEBUG_PAIRS):
            a, b = pair
            self.selection.addItem(f"{index}: M{a[0]} {a[1]} / M{b[0]} {b[1]}", index)
        self.map_channels = W.QCheckBox("联动接收通道")
        defaults = MotorBases()
        base_labels = [("n_base", "N_base (RPM)"), ("i_base", "I_base (A)"), ("v_base", "V_base (V)"),
                       ("p_base", "P_base (W)"), ("t_base", "T_base (N·m)")]
        self.base_widgets = {motor: {key: decimal(getattr(defaults, key), decimals=12, display_decimals=7)
                                     for key, _ in base_labels} for motor in ("motor1", "motor2")}
        self.build_controls(owner.use_dyno_preset, base_labels,
                            [("电机 1 给定转速", self.rpm), ("电机 2 电流幅值", self.imag_ref),
                             ("电机 2 电流角度", self.imag_pos)], "启动对拖", "停止对拖")

    def settings(self):
        bases = {motor: MotorBases(**{key: widget.value() for key, widget in widgets.items()})
                 for motor, widgets in self.base_widgets.items()}
        return DynoSettings(**bases, rpm=self.rpm.value(), imag_ref=self.imag_ref.value(),
                            imag_pos=self.imag_pos.value(), selection=self.selection.currentData(),
                            map_channels=self.map_channels.isChecked())

    def load_profile(self, profile):
        self.loading = True
        error = ""
        try:
            s = DynoSettings.from_profile(profile)
        except (TypeError, ValueError) as exc:
            s, error = DynoSettings(), str(exc)
        self.preset.setCurrentIndex(1 if profile.tx.data_type == "uint32" else 0)
        for motor, widgets in self.base_widgets.items():
            for key, widget in widgets.items():
                widget.setValue(getattr(getattr(s, motor), key))
        self.rpm.setValue(s.rpm)
        self.imag_ref.setValue(s.imag_ref)
        self.imag_pos.setValue(s.imag_pos)
        self.selection.setCurrentIndex(self.selection.findData(s.selection))
        self.map_channels.setChecked(s.map_channels)
        self.loading = False
        self.reset_connection()
        self.hex_preview.clear()
        if error:
            self.message.setText(error)
        self.update_buttons()


class MainWindow(W.QMainWindow):
    def __init__(self, *, display_preferences=None):
        super().__init__()
        self.display_preferences = display_preferences if display_preferences is not None else QtCore.QSettings(
            QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, "SCIScope", "Display")
        try:
            points = self.display_preferences.value("buffer_points", DEFAULT_BUFFER_POINTS, type=int)
        except (TypeError, ValueError):
            points = DEFAULT_BUFFER_POINTS
        self.saved_buffer_points = points if 1 <= points <= MAX_BUFFER_POINTS else DEFAULT_BUFFER_POINTS
        self.setWindowTitle(f"SCI Scope · F28379D 串口工具 {__version__}")
        self.resize(1400, 900)
        self.profile = Profile()
        self.worker = None
        self.hub = None
        self.recorders = []
        self.jobs = []
        self.foc_tool = None
        self.dyno_tool = None
        self.model_sidebar_width = 480
        self.paused_snapshot = None
        self.history_reader = None
        self.history_cache = None
        self.matlab_launch = None
        self.matlab_pending = False
        self.last_bytes = 0
        self.last_rate_time = time.monotonic()
        central = W.QWidget()
        layout = W.QVBoxLayout(central)
        layout.setContentsMargins(12, 8, 12, 8)
        self._build_connection(layout)
        self.tabs = W.QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._build_capture()
        self._build_foc()
        self._build_dyno()
        self._build_history()
        self._build_config()
        self.tabs.currentChanged.connect(self.place_scope)
        self.load_widgets(self.profile)
        self.status = W.QLabel("未连接")
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.record_message)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(33)
        self.periodic = QtCore.QTimer(self)
        self.periodic.timeout.connect(self.send_generic)
        self.matlab_timer = QtCore.QTimer(self)
        self.matlab_timer.setInterval(500)
        self.matlab_timer.timeout.connect(self.check_matlab_import)
        self.interval.valueChanged.connect(self.periodic.setInterval)
        self.refresh_ports()
        self.set_connection_controls(False)

    def _build_connection(self, layout):
        row = W.QHBoxLayout()
        title = W.QLabel("SCI Scope")
        title.setObjectName("title")
        self.port = W.QComboBox()
        self.port.setEditable(True)
        self.port.setInsertPolicy(W.QComboBox.InsertPolicy.NoInsert)
        self.port.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.port.setMinimumContentsLength(24)
        self.port.setMinimumWidth(220)
        self.port.setMaximumWidth(360)
        self.port.currentTextChanged.connect(self.port.setToolTip)
        self.port.currentIndexChanged.connect(lambda: self.port.lineEdit().setCursorPosition(0))
        self.baud = integer(12_000_000, 1, 100_000_000)
        self.simulation = W.QCheckBox("模拟数据")
        self.connect_button = button("连接", self.toggle_connection)
        self.port_refresh = button("刷新", self.refresh_ports)
        for widget in [title, W.QLabel("端口"), self.port, self.port_refresh, W.QLabel("波特率"),
                       self.baud, self.simulation, self.connect_button]:
            row.addWidget(widget)
        row.addStretch()
        self.record_button = button("开始记录…", self.toggle_recording)
        self.record_message = W.QLabel("记录关闭")
        self.record_message.setMaximumWidth(480)
        self.record_message.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.record_button)
        layout.addLayout(row)
        self.connection_notice = W.QLabel()
        self.connection_notice.setWordWrap(True)
        self.connection_notice.setStyleSheet("color: #b45309;")
        self.connection_notice.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.connection_notice)
        self.connection_notice.hide()

    def _build_capture(self):
        self.capture_page = W.QWidget()
        layout = W.QVBoxLayout(self.capture_page)
        splitter = SidebarSplitter()
        self.capture_scope_host = W.QWidget()
        self.capture_scope_layout = W.QVBoxLayout(self.capture_scope_host)
        self.capture_scope_layout.setContentsMargins(0, 0, 0, 0)
        self.live_scope = ScopeWidget()
        self.live_scope.buffer_points.setValue(self.saved_buffer_points)
        self.live_scope.buffer_points.valueChanged.connect(self.resize_display_buffer)
        self.si_units = self.live_scope.units
        self.si_units.show()
        self.si_units.toggled.connect(self.scope_units_changed)
        self.live_scope.paused.toggled.connect(self.pause_display)
        self.capture_scope_layout.addWidget(self.live_scope)
        splitter.addWidget(self.capture_scope_host)
        generic = W.QGroupBox("发送 → 目标 SCI_RCV")
        generic_layout = W.QVBoxLayout(generic)
        self.tx_values = table(["名称", "单位", "数值"])
        generic_layout.addWidget(self.tx_values, 1)
        self.send_button = button("发送一次", self.send_generic)
        generic_layout.addWidget(self.send_button)
        send_row = W.QHBoxLayout()
        self.interval = integer(100, 10, 60000)
        self.interval.setSuffix(" ms")
        self.periodic_check = W.QCheckBox("周期发送")
        self.periodic_check.toggled.connect(self.set_periodic)
        send_row.addWidget(self.periodic_check)
        send_row.addWidget(self.interval)
        generic_layout.addLayout(send_row)
        generic_layout.addWidget(button("预览发送字节", self.preview_tx))
        self.hex_preview = W.QPlainTextEdit()
        self.hex_preview.setReadOnly(True)
        self.hex_preview.setMaximumHeight(110)
        generic_layout.addWidget(self.hex_preview)
        splitter.addWidget(generic)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1000, 310])
        generic.setMinimumWidth(290)
        layout.addWidget(splitter, 1)
        self.tabs.addTab(self.capture_page, "通用收发")

    def _build_foc(self):
        (self.foc_page, self.foc_scope_host, self.foc_scope_layout,
         self.foc_tool, self.foc_sidebar, self.foc_splitter) = self._build_model_page(FocControlPanel, "单电机 FOC")

    def _build_dyno(self):
        (self.dyno_page, self.dyno_scope_host, self.dyno_scope_layout,
         self.dyno_tool, self.dyno_sidebar, self.dyno_splitter) = self._build_model_page(DynoControlPanel, "对拖 FOC")

    def _build_model_page(self, panel_type, title):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        splitter = SidebarSplitter()
        scope_host = W.QWidget()
        scope_layout = W.QVBoxLayout(scope_host)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(scope_host)
        panel = panel_type(self)
        panel.layout().setSpacing(8)
        panel.setAutoFillBackground(True)
        panel.setBackgroundRole(QtGui.QPalette.ColorRole.Base)
        controls = W.QScrollArea()
        controls.setWidgetResizable(True)
        controls.setFrameShape(W.QFrame.Shape.NoFrame)
        controls.setBackgroundRole(QtGui.QPalette.ColorRole.Base)
        controls.viewport().setBackgroundRole(QtGui.QPalette.ColorRole.Base)
        controls.viewport().setAutoFillBackground(True)
        controls.setWidget(panel)
        controls.setMinimumWidth(260)
        splitter.addWidget(controls)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1000, self.model_sidebar_width])
        splitter.splitterMoved.connect(lambda *_: self.remember_model_sidebar_width(splitter))
        layout.addWidget(splitter, 1)
        self.tabs.addTab(page, title)
        return page, scope_host, scope_layout, panel, controls, splitter

    def remember_model_sidebar_width(self, splitter):
        self.model_sidebar_width = splitter.widget(1).width()

    def restore_model_sidebar_width(self):
        page = self.tabs.currentWidget()
        if page is self.foc_page:
            splitter = self.foc_splitter
        elif page is self.dyno_page:
            splitter = self.dyno_splitter
        else:
            return
        width = self.model_sidebar_width
        splitter.setSizes([max(0, splitter.width() - splitter.handleWidth() - width), width])

    def place_scope(self, *_):
        page = self.tabs.currentWidget()
        if page is self.capture_page:
            host, layout = self.capture_scope_host, self.capture_scope_layout
        elif page is self.foc_page:
            host, layout = self.foc_scope_host, self.foc_scope_layout
        elif page is self.dyno_page:
            host, layout = self.dyno_scope_host, self.dyno_scope_layout
        else:
            return
        if self.live_scope.parentWidget() is not host:
            self.live_scope.parentWidget().layout().removeWidget(self.live_scope)
            layout.addWidget(self.live_scope)
            self.live_scope.show()
        if page in (self.foc_page, self.dyno_page):
            QtCore.QTimer.singleShot(0, self.restore_model_sidebar_width)

    def _build_config(self):
        scroll = W.QScrollArea()
        scroll.setWidgetResizable(True)
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        self.config_toolbar = W.QWidget()
        toolbar = W.QHBoxLayout(self.config_toolbar)
        toolbar.setContentsMargins(0, 0, 0, 0)
        for item in [button("默认配置", self.use_default_profile), button("加载配置…", self.load_profile),
                     button("保存配置…", self.save_profile), button("应用配置", self.apply_config)]:
            toolbar.addWidget(item)
        toolbar.addStretch()
        layout.addWidget(self.config_toolbar)
        self.config_body = W.QWidget()
        body = W.QVBoxLayout(self.config_body)
        body.setContentsMargins(0, 0, 0, 0)
        serial_row = W.QHBoxLayout()
        self.profile_name = line()
        self.byte_order = combo(["little", "big"])
        self.data_bits = combo(["5", "6", "7", "8"], "8")
        self.parity = combo(["N", "E", "O", "M", "S"])
        self.stop_bits = combo(["1", "1.5", "2"])
        for label, widget in [("配置名称", self.profile_name), ("字节序", self.byte_order), ("数据位", self.data_bits),
                              ("校验", self.parity), ("停止位", self.stop_bits)]:
            serial_row.addWidget(W.QLabel(label))
            serial_row.addWidget(widget, 1 if widget is self.profile_name else 0)
        body.addLayout(serial_row)
        formats = W.QHBoxLayout()
        tx = W.QGroupBox("发送格式 · SCI_RCV")
        tx_form = W.QFormLayout(tx)
        self.sci_module = combo(list("ABCD"))
        self.tx_header, self.tx_tail = line(), line()
        self.tx_header_mode, self.tx_tail_mode = combo(["ASCII", "HEX"]), combo(["ASCII", "HEX"])
        self.tx_type = combo(list(TYPES))
        self.tx_length_option = combo(["Length via dialog", "Length via input port（未支持）", "Variable length（未支持）"])
        for i in (1, 2):
            self.tx_length_option.model().item(i).setEnabled(False)
        self.tx_length = integer(2, 1, 4096)
        self.tx_fields = table(["名称", "单位", "比例", "偏移"])
        self.tx_fields.setMinimumHeight(150)
        self.tx_length.valueChanged.connect(lambda n: self.resize_fields(self.tx_fields, n, "Data"))
        self.tx_size = W.QLabel()
        for label, widget in [("SCI module", self.sci_module), ("Additional package header", self.marker_input(self.tx_header, self.tx_header_mode)),
                              ("Additional package terminator", self.marker_input(self.tx_tail, self.tx_tail_mode)), ("Data type", self.tx_type),
                              ("Data length option", self.tx_length_option), ("Data length（元素数）", self.tx_length), ("帧长度", self.tx_size)]:
            tx_form.addRow(label, widget)
        formats.addWidget(tx, 1)
        rx = W.QGroupBox("接收格式 · SCI_TX")
        rx_form = W.QFormLayout(rx)
        self.rx_header, self.rx_tail = line(), line()
        self.rx_header_mode, self.rx_tail_mode = combo(["ASCII", "HEX"]), combo(["ASCII", "HEX"])
        self.rx_type = combo(list(TYPES))
        self.rx_count = integer(2, 1, 256)
        self.rx_n = integer(600, 1, 1000000)
        self.rx_fs = decimal(20000)
        self.rx_layout = combo(["交错（每组按通道）", "分通道（每路连续）"])
        self.rx_fields = table(["名称", "单位", "比例", "偏移", "定点小数位", "有符号位流 (0/1)"])
        self.rx_fields.setMinimumHeight(150)
        self.rx_type.currentTextChanged.connect(self.update_rx_conversion_fields)
        self.rx_count.valueChanged.connect(lambda n: self.resize_fields(self.rx_fields, n, "CH"))
        self.rx_size = W.QLabel()
        for label, widget in [("Additional package header", self.marker_input(self.rx_header, self.rx_header_mode)),
                              ("Additional package terminator", self.marker_input(self.rx_tail, self.rx_tail_mode)), ("Data type", self.rx_type),
                              ("通道数", self.rx_count), ("每帧采样组数", self.rx_n), ("采样率 (Hz)", self.rx_fs),
                              ("载荷排列", self.rx_layout), ("Data length", self.rx_size)]:
            rx_form.addRow(label, widget)
        formats.addWidget(rx, 1)
        body.addLayout(formats)
        self.bandwidth_label = W.QLabel()
        self.bandwidth_label.setWordWrap(True)
        body.addWidget(self.bandwidth_label)
        metadata = W.QGroupBox("字段名称与换算")
        metadata_body = W.QWidget()
        columns = W.QHBoxLayout(metadata_body)
        for caption, widget in [("发送字段", self.tx_fields), ("接收通道", self.rx_fields)]:
            column = W.QVBoxLayout()
            column.addWidget(W.QLabel(caption))
            column.addWidget(widget)
            columns.addLayout(column, 1)
        W.QVBoxLayout(metadata).addWidget(metadata_body)
        body.addWidget(metadata)
        body.addStretch()
        layout.addWidget(self.config_body)
        scroll.setWidget(page)
        self.config_page = scroll
        self.tabs.addTab(scroll, "协议配置")

    @staticmethod
    def marker_input(editor, mode):
        widget = W.QWidget()
        row = W.QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(editor, 1)
        row.addWidget(mode)
        return widget

    def set_connection_controls(self, connected):
        self.config_body.setEnabled(not connected)
        self.config_toolbar.setEnabled(not connected)
        for widget in (self.baud, self.port, self.simulation, self.port_refresh):
            widget.setEnabled(not connected)
        for widget in (self.send_button, self.periodic_check, self.record_button):
            widget.setEnabled(connected)
        self.foc_tool.update_buttons()
        self.dyno_tool.update_buttons()

    def _build_history(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        row = W.QHBoxLayout()
        self.history_segment = W.QComboBox()
        self.history_segment.currentIndexChanged.connect(self.select_history)
        row.addWidget(button("打开会话目录…", self.open_history))
        row.addWidget(self.history_segment, 1)
        self.matlab_button = button("用 MATLAB 打开", self.open_history_in_matlab)
        self.matlab_button.setToolTip("打开当前记录段的所选时间区间和勾选通道")
        self.matlab_button.setEnabled(False)
        row.addWidget(self.matlab_button)
        layout.addLayout(row)
        options = W.QHBoxLayout()
        self.history_start = decimal(0, 0, 1e12)
        self.history_end = decimal(1, 0, 1e12)
        for item in [W.QLabel("起点 (s)"), self.history_start, W.QLabel("终点 (s)"), self.history_end, button("加载区间", self.load_history_window),
                     button("统计", self.analyze_history), button("FFT", self.fft_history), button("导出 CSV…", lambda: self.export_history("csv")),
                     button("导出 MAT…", lambda: self.export_history("mat"))]:
            options.addWidget(item)
        layout.addLayout(options)
        self.history_scope = ScopeWidget(live=False)
        layout.addWidget(self.history_scope, 1)
        self.analysis_table = table(["通道", "均值", "最小", "最大", "峰峰值", "RMS"])
        self.analysis_table.setMaximumHeight(180)
        self.analysis_table.hide()
        layout.addWidget(self.analysis_table)
        self.history_message = W.QLabel("未打开记录")
        self.history_message.setWordWrap(True)
        layout.addWidget(self.history_message)
        self.history_page = page
        self.tabs.addTab(page, "记录分析")

    def resize_fields(self, widget, count, prefix):
        old = widget.rowCount()
        widget.setRowCount(count)
        for i in range(old, count):
            fill_row(widget, i, [f"{prefix}{i + 1}", "", 1, 0] + ([0, 0] if widget.columnCount() == 6 else []))
        if widget is self.rx_fields:
            self.update_rx_conversion_fields()

    def rx_conversion_options(self):
        kind = TYPES[self.rx_type.currentText()][0]
        return kind in "iu", kind == "u"

    def update_rx_conversion_fields(self):
        options = self.rx_conversion_options()
        editable = QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsEditable
        for row in range(self.rx_fields.rowCount()):
            for col, enabled in enumerate(options, 4):
                item = self.rx_fields.item(row, col)
                if item is None:
                    continue
                if not enabled:
                    item.setText("0")
                item.setFlags(item.flags() | editable if enabled else item.flags() & ~editable)
                item.setData(QtCore.Qt.ItemDataRole.ForegroundRole, None if enabled else QtGui.QColor("#8a94a4"))
                item.setData(QtCore.Qt.ItemDataRole.BackgroundRole, None if enabled else QtGui.QColor("#edf1f7"))
                item.setToolTip("" if enabled else ("浮点数无需定点小数位" if col == 4 else "仅无符号整数需要有符号位流配置"))

    def load_widgets(self, p):
        p = p.clone()
        self.profile = p
        self.hub = None
        self.paused_snapshot = None
        self.profile_name.setText(p.name)
        self.select_port(p.serial.port)
        self.baud.setValue(p.serial.baudrate)
        for widget, value in [(self.byte_order, p.serial.byte_order), (self.data_bits, p.serial.bytesize), (self.parity, p.serial.parity), (self.stop_bits, f"{p.serial.stopbits:g}"),
                              (self.sci_module, p.tx.sci_module), (self.tx_type, p.tx.data_type), (self.tx_header_mode, p.tx.header_mode), (self.tx_tail_mode, p.tx.terminator_mode),
                              (self.rx_type, p.rx.data_type), (self.rx_header_mode, p.rx.header_mode), (self.rx_tail_mode, p.rx.terminator_mode)]:
            widget.setCurrentText(str(value))
        self.tx_header.setText(p.tx.header)
        self.tx_tail.setText(p.tx.terminator)
        self.tx_length.setValue(p.tx.data_length)
        self.resize_fields(self.tx_fields, p.tx.data_length, "Tx")
        for i, c in enumerate(p.tx.fields):
            fill_row(self.tx_fields, i, [c.name, c.unit, c.scale, c.offset])
        self.rx_header.setText(p.rx.header)
        self.rx_tail.setText(p.rx.terminator)
        self.rx_count.setValue(p.rx.channels)
        self.rx_layout.setCurrentIndex(0 if p.rx.layout == "interleaved" else 1)
        self.rx_n.setValue(p.rx.samples_per_frame)
        self.rx_fs.setValue(p.rx.sample_rate)
        self.resize_fields(self.rx_fields, p.rx.channels, "Channel")
        fractional, signed = self.rx_conversion_options()
        for i, c in enumerate(p.rx.channel_info):
            if not fractional:
                c.fractional_bits = 0
            if not signed:
                c.signed_bits = False
            fill_row(self.rx_fields, i, [c.name, c.unit, c.scale, c.offset, c.fractional_bits, int(c.signed_bits)])
        self.update_rx_conversion_fields()
        if self.foc_tool:
            self.foc_tool.load_profile(p)
        if self.dyno_tool:
            self.dyno_tool.load_profile(p)
        self.interval.setValue(p.tx.interval_ms)
        self.populate_tx_values(p, reset=True)
        self.live_scope.configure(self.scope_profile(p))
        self.show_budget(p)

    def collect_profile(self):
        self.update_rx_conversion_fields()
        p = self.profile.clone()
        p.name = self.profile_name.text()
        s, tx, rx = p.serial, p.tx, p.rx
        s.port, s.baudrate = self.selected_port(), self.baud.value()
        s.byte_order, s.bytesize = self.byte_order.currentText(), int(self.data_bits.currentText())
        s.parity, s.stopbits = self.parity.currentText(), float(self.stop_bits.currentText())
        tx.sci_module = self.sci_module.currentText()
        tx.header, tx.terminator = self.tx_header.text(), self.tx_tail.text()
        tx.header_mode, tx.terminator_mode = self.tx_header_mode.currentText(), self.tx_tail_mode.currentText()
        tx.data_type, tx.data_length = self.tx_type.currentText(), self.tx_length.value()
        tx.length_option = "Length via dialog"
        tx.fields = [Channel(cell(self.tx_fields, i, 0), cell(self.tx_fields, i, 1), float(cell(self.tx_fields, i, 2)), float(cell(self.tx_fields, i, 3))) for i in range(tx.data_length)]
        tx.send_mode = "manual"  # Periodic sending is an explicit live action, never auto-started.
        tx.interval_ms = self.interval.value()
        rx.header, rx.terminator = self.rx_header.text(), self.rx_tail.text()
        rx.header_mode, rx.terminator_mode = self.rx_header_mode.currentText(), self.rx_tail_mode.currentText()
        rx.data_type, rx.channels = self.rx_type.currentText(), self.rx_count.value()
        rx.samples_per_frame, rx.sample_rate, rx.layout = self.rx_n.value(), self.rx_fs.value(), "interleaved" if self.rx_layout.currentIndex() == 0 else "planar"
        rx.channel_info = [Channel(cell(self.rx_fields, i, 0), cell(self.rx_fields, i, 1), float(cell(self.rx_fields, i, 2)), float(cell(self.rx_fields, i, 3)),
                                   int(cell(self.rx_fields, i, 4)), bool(int(cell(self.rx_fields, i, 5)))) for i in range(rx.channels)]
        p.validate()
        return p

    def show_budget(self, p):
        from .config import dtype_for
        rate, ratio = p.bandwidth()
        payload = dtype_for(p.tx.data_type, p.serial.byte_order).itemsize * p.tx.data_length
        size = payload + len(marker_bytes(p.tx.header, p.tx.header_mode)) + len(marker_bytes(p.tx.terminator, p.tx.terminator_mode))
        self.tx_size.setText(f"载荷 {payload} B / 完整帧 {size} B")
        rx_elements = p.rx.channels * p.rx.samples_per_frame
        self.rx_size.setText(f"{rx_elements:,} 个元素 = {rx_elements * dtype_for(p.rx.data_type, p.serial.byte_order).itemsize:,} B 载荷")
        self.setWindowTitle(f"SCI Scope · {p.name} · {__version__}")
        text = f"上报 {rate / 1000:.3f} KB/s · 线路占用 {ratio:.2%} · 每帧覆盖 {p.rx.samples_per_frame / p.rx.sample_rate * 1000:.3f} ms"
        if ratio > 0.8:
            text += " · 带宽余量不足"
        self.bandwidth_label.setText(text)
        self.foc_tool.update_buttons()
        self.dyno_tool.update_buttons()

    def resize_display_buffer(self, points):
        scope = self.live_scope
        previous = self.hub.ring.capacity if self.hub else self.saved_buffer_points
        try:
            if self.hub:
                self.hub.resize_buffer(points)
            else:
                ScopeSettings(points).validate(self.profile)
        except (MemoryError, ValueError) as exc:
            blocker = QtCore.QSignalBlocker(scope.buffer_points)
            scope.buffer_points.setValue(previous)
            del blocker
            self.notify(str(exc))
            return
        self.saved_buffer_points = points
        self.display_preferences.setValue("buffer_points", points)
        self.display_preferences.sync()
        scope.update_buffer_settings()
        scope.capture_key = None
        if not scope.paused.isChecked():
            self.refresh()

    def populate_tx_values(self, p, reset=False):
        old = [] if reset else [cell(self.tx_values, i, 2, "0") for i in range(self.tx_values.rowCount())]
        self.tx_values.setRowCount(p.tx.data_length)
        for i, c in enumerate(p.tx.fields):
            default = "0"
            fill_row(self.tx_values, i, [c.name, c.unit, old[i] if i < len(old) else default])
            for j in (0, 1):
                self.tx_values.item(i, j).setFlags(self.tx_values.item(i, j).flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)

    def apply_config(self):
        if self.worker:
            self.notify("请先断开连接再修改帧结构")
            return
        try:
            p = self.collect_profile()
            self.profile = p
            self.hub = None
            self.paused_snapshot = None
            self.populate_tx_values(p)
            self.live_scope.configure(self.scope_profile(p))
            self.show_budget(p)
        except Exception as exc:
            self.notify(str(exc))

    def apply_preset(self, profile):
        if self.worker:
            self.notify("请先断开连接")
            return
        profile.serial.port = self.selected_port()
        self.load_widgets(profile)
        return True

    def use_default_profile(self):
        self.apply_preset(Profile())

    def use_foc_preset(self, fixed=False):
        if self.apply_preset(official_profile(fixed)):
            self.tabs.setCurrentWidget(self.foc_page)

    def use_dyno_preset(self, fixed=False):
        if self.apply_preset(dyno_profile(fixed)):
            self.tabs.setCurrentWidget(self.dyno_page)

    def command_sent(self, origin, text):
        if origin == "generic":
            self.hex_preview.setPlainText("已发送\n" + text)
        elif origin in ("foc:start", "foc:stop", "foc:update"):
            self.foc_tool.hex_preview.setPlainText(text)
            action = {"foc:start": "启动", "foc:stop": "停止", "foc:update": "更新"}[origin]
            self.foc_tool.message.setText(action + "命令已发送")
        elif origin in ("dyno:start", "dyno:stop", "dyno:update"):
            self.dyno_tool.hex_preview.setPlainText(text)
            action = {"dyno:start": "启动", "dyno:stop": "停止", "dyno:update": "更新"}[origin]
            self.dyno_tool.message.setText(action + "命令已发送")

    def load_profile(self):
        if self.worker:
            self.notify("请先断开连接")
            return
        path, _ = W.QFileDialog.getOpenFileName(self, "加载协议配置", "", "JSON (*.json)")
        if path:
            try:
                self.load_widgets(Profile.load(path))
            except Exception as exc:
                self.notify(str(exc))

    def save_profile(self):
        path, _ = W.QFileDialog.getSaveFileName(self, "保存协议配置", "profile.json", "JSON (*.json)")
        if path:
            try:
                self.collect_profile().save(path)
            except Exception as exc:
                self.notify(str(exc))

    def selected_port(self):
        text = self.port.currentText().strip()
        index = self.port.findText(text)
        return self.port.itemData(index) if index >= 0 else text

    def select_port(self, device):
        index = self.port.findData(device)
        if index >= 0:
            self.port.setCurrentIndex(index)
            self.port.setEditText(self.port.itemText(index))
        else:
            self.port.setCurrentIndex(-1)
            self.port.setCurrentText(device)
        self.port.lineEdit().setCursorPosition(0)

    def refresh_ports(self):
        previous = self.selected_port()
        ports = sorted(list_ports.comports())
        self.port.clear()
        preferred = None
        for info in ports:
            description = (info.description or "").strip()
            description = re.sub(rf"\s*\({re.escape(info.device)}\)\s*$", "", description, flags=re.IGNORECASE).strip()
            if description.casefold() in ("n/a", info.device.casefold()):
                description = ""
            label = f"{info.device}（{description}）" if description else info.device
            self.port.addItem(label, info.device)
            self.port.setItemData(self.port.count() - 1, label, QtCore.Qt.ItemDataRole.ToolTipRole)
            if preferred is None and re.sub(r"\s+", "", description).casefold().startswith("xds100classusbserial"):
                preferred = info.device
        label_width = max((self.port.fontMetrics().horizontalAdvance(self.port.itemText(i)) for i in range(self.port.count())), default=180) + 40
        self.port.setMinimumWidth(min(360, max(220, label_width)))
        self.port.view().setMinimumWidth(label_width)
        self.select_port(previous or preferred or (ports[0].device if ports else ""))

    def toggle_connection(self):
        if self.worker:
            self.disconnect()
            return
        try:
            p = self.collect_profile()
            if not self.simulation.isChecked() and not p.serial.port:
                raise ValueError("请选择 COM 端口，或使用模拟数据源")
            self.profile = p
            self.populate_tx_values(p)
            self.show_budget(p)
            self.live_scope.configure(self.scope_profile(p))
            self.hub = DataHub(p, buffer_points=self.live_scope.buffer_points.value())
            self.paused_snapshot = None
            self.connection_notice.clear()
            self.connection_notice.hide()
            self.connection_error = ""
            self.worker = AcquisitionWorker(p, self.hub, self.simulation.isChecked(), self)
            self.worker.error.connect(self.acquisition_error)
            self.worker.connected.connect(lambda name: self.status.setText(f"已连接 {name}"))
            self.worker.tagged_command_sent.connect(self.command_sent)
            self.worker.finished.connect(self.acquisition_finished)
            self.set_connection_controls(True)
            if self.foc_tool:
                self.foc_tool.reset_connection()
            if self.dyno_tool:
                self.dyno_tool.reset_connection()
            if self.tabs.currentWidget() not in (self.capture_page, self.foc_page, self.dyno_page):
                self.tabs.setCurrentWidget(self.capture_page)
            self.connect_button.setText("断开")
            self.worker.start()
            self.last_bytes, self.last_rate_time = 0, time.monotonic()
        except Exception as exc:
            self.worker = None
            self.notify(str(exc))

    def disconnect(self):
        self.periodic_check.setChecked(False)
        if self.hub:
            recorder = self.hub.stop_recording()
            if recorder:
                self.recorders.append(recorder)
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
            if self.worker.isRunning():
                self.notify("串口线程仍在结束，请稍后重试")
                return
            self.worker.deleteLater()
            self.worker = None
        self.acquisition_finished()

    def acquisition_error(self, message):
        self.connection_error = message
        self.status.setText(message)
        if self.foc_tool:
            self.foc_tool.reset_connection()
        if self.dyno_tool:
            self.dyno_tool.reset_connection()

    def acquisition_finished(self):
        self.periodic.stop()
        self.periodic_check.setChecked(False)
        if self.hub:
            recorder = self.hub.stop_recording()
            if recorder:
                self.recorders.append(recorder)
        if self.worker and not self.worker.isRunning():
            self.worker.deleteLater()
            self.worker = None
        self.set_connection_controls(False)
        if self.foc_tool:
            self.foc_tool.reset_connection()
        if self.dyno_tool:
            self.dyno_tool.reset_connection()
        self.connect_button.setText("连接")
        self.record_button.setText("开始记录…")
        self.connection_notice.clear()
        self.connection_notice.hide()
        self.status.setText(getattr(self, "connection_error", "") or "未连接")

    def scope_profile(self, p):
        p = p.clone()
        if not self.si_units.isChecked():
            for c in p.rx.channel_info:
                c.unit = "解码值"
        return p

    def scope_units_changed(self):
        scope = self.live_scope
        p = self.paused_snapshot[3] if scope.paused.isChecked() and self.paused_snapshot else self.hub.profile if self.hub else self.profile
        limits = scope.plots[0].viewRange()[0] if scope.plots else None
        window_seconds = scope.window.value()
        self.live_scope.configure(self.scope_profile(p))
        if scope.paused.isChecked() and self.paused_snapshot:
            start, raw, valid, p = self.paused_snapshot
            t = (np.arange(len(raw)) + start) / p.rx.sample_rate
            scope.freeze_data(t, physical_values(raw, p, self.si_units.isChecked()), valid)
            scope.window.setValue(window_seconds)
            if limits and scope.plots:
                scope.plots[0].setXRange(*limits, padding=0)

    def pause_display(self, paused):
        scope = self.live_scope
        if not paused:
            self.paused_snapshot = None
            scope.update_buffer_settings()
            scope.capture_key = None
            self.refresh()
            return
        if self.hub is None:
            return
        # Freeze history up to the last displayed sample, in one epoch/metadata lock.
        with self.hub.lock:
            p = self.hub.profile.clone()
            if scope.capture_key and scope.capture_key[:2] != (id(self.hub.ring), self.hub.segment):
                return  # The displayed epoch is no longer in the live ring.
            begin, total = self.hub.ring.bounds()
            end = min(total, int(round(scope.last_t[-1] * p.rx.sample_rate)) + 1) if len(scope.last_t) else total
            start, raw, valid = self.hub.ring.snapshot_range(begin, end)
        if len(raw):
            self.paused_snapshot = (start, raw, valid, p)
            t = (np.arange(len(raw)) + start) / p.rx.sample_rate
            scope.freeze_data(t, physical_values(raw, p, self.si_units.isChecked()), valid)

    def generic_payload(self):
        p = self.profile if self.worker else self.collect_profile()
        if not self.worker:
            self.populate_tx_values(p)
            self.show_budget(p)
        return encode_values(p, [cell(self.tx_values, i, 2, "0") for i in range(self.tx_values.rowCount())])

    def preview_tx(self):
        try:
            payload = self.generic_payload()
            self.hex_preview.setPlainText(f"{len(payload)} 字节\n{payload.hex(' ').upper()}")
        except Exception as exc:
            self.notify(str(exc))

    def send_generic(self):
        try:
            if not self.worker or not self.worker.isRunning():
                raise ValueError("请先连接")
            payload = self.generic_payload()
            self.worker.send(payload, origin="generic")
        except Exception as exc:
            self.periodic_check.setChecked(False)
            self.notify(str(exc))

    def set_periodic(self, enabled):
        if enabled:
            if not self.worker or not self.worker.isRunning():
                self.periodic_check.setChecked(False)
                self.notify("请先连接，再启用周期发送")
                return
            self.periodic.start(self.interval.value())
        else:
            self.periodic.stop()

    def toggle_recording(self):
        if not self.worker or not self.worker.isRunning():
            self.notify("请先连接采集源")
            return
        if self.hub.recorder:
            recorder = self.hub.stop_recording()
            self.recorders.append(recorder)
            self.record_button.setText("开始记录…")
            self.record_message.setText("正在完成写盘…")
            return
        parent = W.QFileDialog.getExistingDirectory(self, "选择记录的父目录")
        if parent:
            path = Path(parent) / datetime.now().strftime("session_%Y%m%d_%H%M%S_%f")
            try:
                self.hub.start_recording(path)
                self.record_button.setText("停止记录")
                self.record_message.setText(str(path))
            except Exception as exc:
                self.notify(str(exc))

    def refresh(self):
        if self.hub:
            p = self.hub.profile
            if not self.live_scope.paused.isChecked() and self.live_scope.profile and self.live_scope.profile.rx.channel_info != self.scope_profile(p).rx.channel_info:
                self.live_scope.configure(self.scope_profile(p))
                self.profile.rx.channel_info = p.clone().rx.channel_info
                for i, c in enumerate(p.rx.channel_info):
                    fill_row(self.rx_fields, i, [c.name, c.unit, c.scale, c.offset, c.fractional_bits, int(c.signed_bits)])
                self.update_rx_conversion_fields()
            if not self.live_scope.paused.isChecked() and self.tabs.currentWidget() in (self.capture_page, self.foc_page, self.dyno_page):
                scope = self.live_scope
                _, total = self.hub.ring.bounds()
                begin, end = scope.capture_rows(p.rx.sample_rate, total)
                key = (id(self.hub.ring), self.hub.segment, begin, end, self.si_units.isChecked())
                if key != scope.capture_key:
                    start, raw, valid = self.hub.ring.snapshot_range(begin, end)
                    values = physical_values(raw, p, self.si_units.isChecked())
                    t = (np.arange(len(raw)) + start) / p.rx.sample_rate
                    scope.update_data(t, values, valid)
                    scope.capture_key = key
                else:
                    scope.set_latest_range()
            now = time.monotonic()
            if now - self.last_rate_time >= 0.5 and self.worker and self.worker.isRunning():
                speed = (self.hub.bytes_received - self.last_bytes) / (now - self.last_rate_time)
                self.last_rate_time, self.last_bytes = now, self.hub.bytes_received
                recorder = self.hub.recorder
                record = f" · 记录队列 {recorder.pending_bytes / 1024:.0f} KiB / 已写 {recorder.written_rows:,} 组" if recorder else ""
                sending = " · 周期发送" if self.periodic.isActive() else ""
                self.status.setText(f"{speed / 1000:.1f} KB/s · {self.hub.frames:,} 帧 · 重同步 {self.hub.resyncs} 次{record}{sending}")
                warning = self.hub.reception_warning(now)
                self.connection_notice.setText(warning)
                self.connection_notice.setVisible(bool(warning))
                if recorder and recorder.failed:
                    self.record_message.setText(recorder.failed)
                    failed = self.hub.stop_recording()
                    if failed:
                        self.recorders.append(failed)
                    self.record_button.setText("开始记录…")
        for recorder in self.recorders[:]:
            if not recorder.is_alive():
                self.record_message.setText(recorder.failed or f"记录完成：{recorder.root}")
                self.recorders.remove(recorder)

    def open_history(self):
        path = W.QFileDialog.getExistingDirectory(self, "打开会话目录")
        if path:
            self.open_session(path)

    def open_session(self, path):
        try:
            files = session_files(path)
            if not files:
                raise ValueError("没有找到记录段")
            self.history_segment.blockSignals(True)
            self.history_segment.clear()
            for file in files:
                self.history_segment.addItem(file.name, str(file))
            self.history_segment.blockSignals(False)
            self.select_history()
        except Exception as exc:
            self.notify(str(exc))

    def select_history(self, *_):
        self.matlab_button.setEnabled(False)
        self.history_reader = None
        self.history_cache = None
        path = self.history_segment.currentData()
        if not path:
            return
        try:
            if self.hub and self.hub.recorder and Path(path).parent == self.hub.recorder.root:
                raise ValueError("请先结束该会话的记录，再打开历史数据")
            self.history_reader = SegmentReader(path)
            self.matlab_button.setEnabled(not self.matlab_pending)
            self.history_cache = None
            self.clear_statistics()
            self.history_scope.configure(self.history_reader.profile)
            self.history_start.setValue(0)
            self.history_end.setValue(min(1, self.history_reader.duration))
            self.history_message.setText(f"{self.history_reader.rows:,} 组 · {self.history_reader.duration:.3f}s · " + ("正常关闭" if self.history_reader.closed_cleanly else "记录不完整"))
            self.load_history_window()
        except Exception as exc:
            self.notify(str(exc))

    def launch_job(self, function, result_handler, error_handler=None):
        if any(worker.isRunning() for worker in self.jobs):
            self.notify("请等待当前后台分析或导出任务完成")
            return False
        worker = JobWorker(function, self)
        worker.result.connect(result_handler)
        worker.error.connect(error_handler or self.notify)
        worker.progress.connect(lambda value: self.history_message.setText(f"后台任务 {value}%"))
        self.jobs.append(worker)
        worker.finished.connect(lambda: self.job_finished(worker))
        worker.start()
        return True

    def job_finished(self, worker):
        if worker in self.jobs:
            self.jobs.remove(worker)
        worker.deleteLater()

    def load_history_window(self):
        if self.history_reader is None:
            return
        reader = self.history_reader
        start, end = self.history_start.value(), self.history_end.value()
        if end <= start:
            self.notify("终点必须大于起点")
            return
        estimate = (end - start) * reader.profile.rx.sample_rate * (reader.profile.rx.channels * 12 + 9)
        if estimate > 128 * 1024**2:
            self.notify("该查看区间预计超过 128 MiB，请缩小区间；导出可处理完整长记录")
            return
        self.clear_statistics()
        self.launch_job(lambda report: (reader.path, start, end, reader.read(start, end)), self.history_loaded)

    def clear_statistics(self):
        self.analysis_table.setRowCount(0)
        self.analysis_table.hide()

    def history_loaded(self, result):
        path, start, end, arrays = result
        if not self.history_reader or self.history_reader.path != path:
            return
        self.history_cache = (start, end, arrays)
        t, data, valid = arrays
        self.history_scope.update_data(t, data, valid)
        if len(t) and self.history_scope.plots:
            self.history_scope.plots[0].setXRange(t[0], max(t[-1], t[0] + 1e-6), padding=0.01)
        self.history_message.setText(f"已加载 {len(t):,} 组 · 有效 {valid.sum():,} 组")

    def require_history(self):
        if self.history_cache is None:
            raise ValueError("请先加载区间")
        start, end, arrays = self.history_cache
        if start != self.history_start.value() or end != self.history_end.value():
            raise ValueError("区间已改变，请重新加载区间")
        return arrays

    def analyze_history(self):
        try:
            _, data, valid = self.require_history()
            profile = self.history_reader.profile.clone()
            self.launch_job(lambda report: (profile, statistics(data, valid)), self.statistics_ready)
        except Exception as exc:
            self.notify(str(exc))

    def statistics_ready(self, result):
        profile, values = result
        self.analysis_table.setRowCount(profile.rx.channels)
        for i, channel in enumerate(profile.rx.channel_info):
            fill_row(self.analysis_table, i, [channel.name] + [f"{values[key][i]:.8g}" for key in ("均值", "最小值", "最大值", "峰峰值", "RMS")])
        self.analysis_table.show()

    def fft_history(self):
        try:
            _, data, valid = self.require_history()
            profile = self.history_reader.profile.clone()
            self.launch_job(lambda report: (profile, amplitude_spectrum(data, profile.rx.sample_rate, valid)), self.fft_ready)
        except Exception as exc:
            self.notify(str(exc))

    def fft_ready(self, result):
        profile, (frequency, amplitude) = result
        dialog = W.QDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("FFT")
        dialog.resize(1000, 600)
        layout = W.QVBoxLayout(dialog)
        plot = pg.PlotWidget()
        plot.addLegend()
        plot.setLabel("bottom", "频率", units="Hz")
        plot.setLabel("left", "幅值")
        for i, channel in enumerate(profile.rx.channel_info):
            if i in self.history_scope.selected():
                curve = plot.plot(frequency, amplitude[:, i], pen=COLORS[i % len(COLORS)], name=channel.name)
                curve.setDownsampling(auto=True, method="peak")
        layout.addWidget(plot)
        dialog.show()

    def export_history(self, kind):
        if self.history_reader is None:
            self.notify("请先打开记录")
            return
        path, _ = W.QFileDialog.getSaveFileName(self, f"导出 {kind.upper()}", f"export.{kind}", f"{kind.upper()} (*.{kind})")
        if path:
            reader = self.history_reader
            start, end = self.history_start.value(), self.history_end.value()
            channels = self.history_scope.selected()
            self.launch_job(lambda report: export_segment(reader, path, kind, start, end, channels, progress=report),
                            lambda files: self.history_message.setText("导出完成：" + ", ".join(str(p) for p in files)))

    def open_history_in_matlab(self):
        if self.history_reader is None:
            self.notify("请先打开记录")
            return
        if self.matlab_pending:
            return
        reader = self.history_reader
        start, end = self.history_start.value(), self.history_end.value()
        channels = self.history_scope.selected()
        started = self.launch_job(
            lambda report: open_matlab_recording(reader, start, end, channels, progress=report),
            self.matlab_started, self.matlab_failed)
        if started:
            self.matlab_pending = True
            self.matlab_button.setEnabled(False)
            self.history_message.setText("正在准备 MATLAB 数据…")

    def matlab_started(self, launch):
        self.matlab_launch = launch
        self.history_message.setText("MATLAB 正在启动并导入…")
        self.matlab_timer.start()

    def check_matlab_import(self):
        try:
            result = self.matlab_launch.result()
            if result is None:
                return
            if not result.get("ok"):
                raise RuntimeError(f"MATLAB 导入失败：{result.get('identifier', '')}\n{result.get('message', '')}")
            viewer = "仿真数据检查器" if result["viewer"] == "sdi" else "绘图窗口"
            self.finish_matlab_import()
            self.history_message.setText(f"已在 MATLAB {viewer}打开 · {result.get('name', '')} · {result['signal_count']} 通道 · {result['samples']:,} 组")
        except Exception as exc:
            self.matlab_failed(str(exc))

    def finish_matlab_import(self):
        self.matlab_timer.stop()
        self.matlab_launch = None
        self.matlab_pending = False
        self.matlab_button.setEnabled(self.history_reader is not None)

    def matlab_failed(self, message):
        self.finish_matlab_import()
        self.history_message.setText("MATLAB 打开失败")
        self.notify(message)

    def notify(self, message):
        W.QMessageBox.warning(self, "SCI Scope", str(message))

    def closeEvent(self, event):
        self.disconnect()
        if self.worker and self.worker.isRunning():
            event.ignore()
            return
        for worker in self.jobs:
            worker.cancelled = True
            if not worker.wait(10000):
                event.ignore()
                return
        for recorder in self.recorders:
            recorder.stop()
            recorder.join(timeout=10)
            if recorder.is_alive():
                event.ignore()
                return
        self.timer.stop()
        self.periodic.stop()
        self.matlab_timer.stop()
        self.live_scope.clear_plots()
        self.history_scope.clear_plots()
        event.accept()


def configure_application(app):
    app.setStyle("Fusion")
    # Offscreen QA does not always discover Windows system fonts automatically.
    # Register installed fonts; no system font files are copied or redistributed.
    if os.name == "nt":
        font_root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            font_file = font_root / name
            if font_file.is_file():
                QtGui.QFontDatabase.addApplicationFont(str(font_file))
    families = QtGui.QFontDatabase.families()
    family = next((name for name in ("Microsoft YaHei UI", "Noto Sans CJK SC", "Noto Sans CJK JP") if name in families),
                  QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.GeneralFont).family())
    app.setFont(QtGui.QFont(family, 10))
    palette = QtGui.QPalette()
    for role, color in [(QtGui.QPalette.ColorRole.Window, "#edf1f7"), (QtGui.QPalette.ColorRole.WindowText, "#17243b"),
                        (QtGui.QPalette.ColorRole.Base, "#ffffff"), (QtGui.QPalette.ColorRole.AlternateBase, "#f2f6fc"),
                        (QtGui.QPalette.ColorRole.Text, "#17243b"), (QtGui.QPalette.ColorRole.Button, "#f4f7fb"),
                        (QtGui.QPalette.ColorRole.ButtonText, "#17243b"), (QtGui.QPalette.ColorRole.Highlight, "#2568b8"),
                        (QtGui.QPalette.ColorRole.HighlightedText, "#ffffff")]:
        palette.setColor(role, QtGui.QColor(color))
    app.setPalette(palette)
    app.setStyleSheet("QGroupBox {font-weight:600; margin-top:12px; padding:16px 10px 10px; border:1px solid #ccd6e4; border-radius:7px;} QGroupBox::title {subcontrol-origin:margin; left:12px;} QPushButton {padding:7px 12px;} QTabBar::tab {padding:10px 22px;} QLabel#title {font-size:23px;font-weight:600;padding:10px;} QHeaderView::section {padding:6px;}")


def run_app(smoke_path=None):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow()
    window.show()
    if smoke_path:
        root = Path(smoke_path)
        root.mkdir(parents=True, exist_ok=False)
        window.simulation.setChecked(True)
        window.toggle_connection()
        window.tabs.setCurrentWidget(window.capture_page)
        recorder = window.hub.start_recording(root / "session")

        def finish_smoke():
            window.grab().save(str(root / "window.png"))
            frames = window.hub.frames
            window.disconnect()
            recorder.join(10)
            report = {"passed": frames > 5 and not recorder.failed, "frames": frames,
                      "rows_written": recorder.written_rows, "error": recorder.failed}
            (root / "smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            window.close()
            app.exit(0 if report["passed"] else 1)

        QtCore.QTimer.singleShot(1500, finish_smoke)
    return app.exec()
