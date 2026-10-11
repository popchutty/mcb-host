import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtWidgets

@pytest.fixture(scope="session", autouse=True)
def qt_application():
    # Keep the application alive until every test widget has been disposed.
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert not app.allWidgets(), "Tests must destroy their windows before QApplication exits"
