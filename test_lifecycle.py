#!/usr/bin/python3
"""Run with dbus-run-session -- python3 test_lifecycle.py; firmware writes mocked."""
import copy
from unittest.mock import MagicMock, patch
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib
import neo_control as neo

# Capture read-only telemetry, then isolate every hardware operation.
data = neo.snapshot()
data['driver'] = {'profile': 5, 'profile_mask': 115, 'cpu_rpm': 5400,
                  'gpu_rpm': 4500, 'cpu_mode': 3, 'gpu_mode': 3,
                  'cpu_percent': 60, 'gpu_percent': 60}
failures = []
base = MagicMock()

def exercise():
    app = Gtk.Application.get_default()
    try:
        assert app.status is not None, 'Initial telemetry unavailable'
        app.manual_owned = True
        target = neo.CoolingTarget45()
        app.cooling_target = target
        app.window.close()
        assert not app.window.get_visible(), 'Close did not hide window'
        assert not app.closing and app.manual_owned, 'Close stopped fan ownership'
        assert app.cooling_target is target, 'Close stopped target cooling'
        control.assert_not_called()
        app.cooling_target = None
        app.keepalive()
        GLib.timeout_add(300, reopen_and_quit)
    except Exception as error:
        failures.append(str(error))
        app.quit()
    return False

def reopen_and_quit():
    app = Gtk.Application.get_default()
    try:
        assert not app.window.get_visible(), 'Hidden window unexpectedly reopened'
        base.__truediv__.return_value.write_text.assert_called_with('1\n')
        original = app.window
        app.activate()
        assert app.window is original and app.window.get_visible(), 'Activation did not reopen same window'
        control.assert_not_called()
        app.activate_action('hide', None)
        assert not app.window.get_visible(), 'Menu hide failed'
        app.activate_action('quit', None)
    except Exception as error:
        failures.append(str(error))
        app.quit()
    return False

def timeout():
    failures.append('Lifecycle timed out')
    Gtk.Application.get_default().quit()
    return False

with patch.object(neo, 'snapshot', side_effect=lambda *args: copy.deepcopy(data)), \
     patch.object(neo, 'BASE', base), \
     patch.object(neo, 'write_control', return_value=data['driver']) as control:
    GLib.timeout_add(700, exercise)
    GLib.timeout_add_seconds(8, timeout)
    result = neo.run_gui()
    assert not failures, failures
    assert result == 0, result
    control.assert_called_once_with('fan_control', 'auto')
print('PASS: Close preserves ownership/target, hidden heartbeat, same-window reopen, explicit Quit restores Auto.')
