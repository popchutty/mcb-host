from __future__ import annotations

import numpy as np


def peak_trace(t: np.ndarray, values: np.ndarray, bins: int = 700) -> tuple[np.ndarray, np.ndarray]:
    """Reduce one visible trace, retaining extrema at their actual sample times."""
    if len(t) <= bins * 2:
        return t, values
    stride = int(np.ceil(len(t) / bins))
    full, remainder = divmod(len(t), stride)
    groups = values[:full * stride].reshape(full, stride)
    finite = np.isfinite(groups)
    if finite.all():
        low, high = groups.argmin(axis=1), groups.argmax(axis=1)
    else:
        low = np.where(finite, groups, np.inf).argmin(axis=1)
        high = np.where(finite, groups, -np.inf).argmax(axis=1)
    # Keep the temporal order, including when a maximum precedes a minimum.
    offsets = np.arange(full) * stride
    pairs = np.column_stack((offsets + np.minimum(low, high), offsets + np.maximum(low, high)))
    indices = np.concatenate(([0], pairs.ravel()))
    if remainder:
        tail = values[full * stride:]
        tail_finite = np.isfinite(tail)
        low = np.where(tail_finite, tail, np.inf).argmin()
        high = np.where(tail_finite, tail, -np.inf).argmax()
        indices = np.concatenate((indices, full * stride + np.sort([low, high])))
    indices = np.concatenate((indices, [len(t) - 1]))
    result = values[indices].copy()
    # An invalid bin must break the trace rather than connect across missing data.
    if not finite.all():
        result[1:1 + full * 2].reshape(full, 2)[~finite.all(axis=1)] = np.nan
    if remainder and not tail_finite.all():
        result[-3:-1] = np.nan
    return t[indices], result


def statistics(data: np.ndarray, valid: np.ndarray | None = None) -> dict[str, np.ndarray]:
    selected = data if valid is None else data[valid]
    if not len(selected):
        raise ValueError("所选区间没有有效样本")
    selected = np.where(np.isfinite(selected), selected, np.nan)
    return {"均值": np.nanmean(selected, axis=0), "最小值": np.nanmin(selected, axis=0),
            "最大值": np.nanmax(selected, axis=0), "峰峰值": np.nanmax(selected, axis=0) - np.nanmin(selected, axis=0),
            "RMS": np.sqrt(np.nanmean(selected**2, axis=0))}


def amplitude_spectrum(data: np.ndarray, sample_rate: float, valid: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    if len(data) < 4:
        raise ValueError("FFT 至少需要四个样本")
    if not np.isfinite(data).all() or valid is not None and not valid.all():
        raise ValueError("FFT 区间包含过渡或非有限样本，请选择连续有效区间")
    # Periodic Hann window; scale by its coherent gain.
    n = len(data)
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)
    centered = data - np.mean(data, axis=0)
    spectrum = np.abs(np.fft.rfft(centered * window[:, None], axis=0)) * 2 / window.sum()
    spectrum[0] /= 2
    if n % 2 == 0:
        spectrum[-1] /= 2
    return np.fft.rfftfreq(n, 1 / sample_rate), spectrum
