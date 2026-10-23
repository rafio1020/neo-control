#!/usr/bin/python3
"""Neo Control: native PHN16-71 fan and firmware profile control.

SPDX-License-Identifier: GPL-3.0-or-later
The application never writes raw EC addresses or runs as root. Hardware writes
are restricted by the companion kernel module and verified against its status.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import time

BASE = Path('/sys/devices/platform/neo-control')
PROFILES = [(6, 'Eco', 'Battery efficiency'), (0, 'Quiet', 'Lower fan noise'),
            (1, 'Balanced', 'Everyday use'), (4, 'Performance', 'More power'),
            (5, 'Turbo', 'Highest firmware mode')]
MODE_NAMES = {1: 'Automatic', 2: 'Maximum', 3: 'Manual'}
LIGHTING_MODES = ['Static', 'Breathing', 'Neon', 'Wave', 'Shifting', 'Zoom', 'Meteor', 'Twinkling']
OPTIONAL_CONTROLS = ('battery_limit', 'lighting', 'zones')


def read_text(path: Path) -> str:
    return path.read_text().strip()


def optional_text(path: Path):
    try:
        return read_text(path)
    except (OSError, UnicodeError):
        return None


def parse_status(raw: str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('The driver returned an invalid status object.')
    ranges = {'profile': range(8), 'profile_mask': range(256),
              'cpu_rpm': range(20001), 'gpu_rpm': range(20001),
              'cpu_mode': MODE_NAMES, 'gpu_mode': MODE_NAMES,
              'cpu_percent': range(101), 'gpu_percent': range(101)}
    for field, allowed in ranges.items():
        if field not in value:
            raise ValueError(f'The driver returned no {field} field.')
        observed = value[field]
        if observed is not None and (type(observed) is not int or observed not in allowed):
            raise ValueError(f'The driver returned an invalid {field}.')
    return value


def read_status() -> dict:
    return parse_status(read_text(BASE / 'status'))


def verify_change(status: dict, target: str, value: str) -> None:
    if target == 'profile':
        if status['profile'] != int(value):
            raise RuntimeError('Firmware did not retain the requested performance mode.')
        return
    parts = value.split()
    mode = {'auto': 1, 'max': 2, 'manual': 3}[parts[0]]
    if any(status[f'{fan}_mode'] != mode for fan in ('cpu', 'gpu')):
        raise RuntimeError('Firmware fan-mode readback differs from the requested mode.')
    if mode == 3:
        for fan, expected in zip(('cpu', 'gpu'), map(int, parts[1:])):
            if status[f'{fan}_percent'] is None or abs(status[f'{fan}_percent'] - expected) > 1:
                raise RuntimeError(f'{fan.upper()} fan setting was not retained by firmware.')


def read_optional(target):
    raw = read_text(BASE / target)
    if target == 'battery_limit':
        if raw not in ('0', '1'):
            raise ValueError('The driver returned an invalid charge limit.')
        return int(raw)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('The driver returned invalid lighting telemetry.')
    if target == 'lighting':
        bounds = {'mode': range(8), 'speed': range(10), 'brightness': range(101),
                  'direction': range(3), 'red': range(256), 'green': range(256), 'blue': range(256)}
        for key, allowed in bounds.items():
            if type(value.get(key)) is not int or value[key] not in allowed:
                raise ValueError(f'The driver returned an invalid lighting {key}.')
    elif target == 'zones':
        colors = value.get('colors')
        if not isinstance(colors, list) or len(colors) != 4 or any(
                not isinstance(color, str) or not re.fullmatch(r'[0-9a-fA-F]{6}', color) for color in colors):
            raise ValueError('The driver returned invalid zone colors.')
        if type(value.get('brightness')) is not int or not 0 <= value['brightness'] <= 100:
            raise ValueError('The driver returned invalid zone brightness.')
        value['colors'] = [color.upper() for color in colors]
    else:
        raise ValueError('Unsupported optional control.')
    return value


def validate_optional_command(target, value):
    if target == 'battery_limit':
        if value not in ('0', '1'):
            raise ValueError('Charge limit must be enabled or disabled.')
        return int(value)
    parts = value.split()
    if target == 'lighting':
        if len(parts) != 7:
            raise ValueError('Lighting requires seven values.')
        values = list(map(int, parts))
        for observed, allowed in zip(values, (range(8), range(10), range(101), (1, 2), range(256), range(256), range(256))):
            if observed not in allowed:
                raise ValueError('A lighting setting is outside its supported range.')
        return dict(zip(('mode', 'speed', 'brightness', 'direction', 'red', 'green', 'blue'), values))
    if target == 'zones':
        if len(parts) != 5 or any(not re.fullmatch(r'[0-9a-fA-F]{6}', color) for color in parts[:4]):
            raise ValueError('Choose four valid six-digit RGB colors.')
        brightness = int(parts[4])
        if not 0 <= brightness <= 100:
            raise ValueError('Brightness must be 0–100%.')
        return {'colors': [color.upper() for color in parts[:4]], 'brightness': brightness}
    raise ValueError('Unsupported optional control.')


def verify_optional(target, expected, observed):
    if target in ('battery_limit', 'zones'):
        if observed != expected:
            raise RuntimeError('Firmware readback did not retain the requested setting.')
        return
    # Verify every active field; only static speed, non-directional modes,
    # and the built-in colors of Neon/Wave are legitimately ignored.
    keys = ['mode', 'brightness']
    if expected['mode'] > 0:
        keys.append('speed')
    if expected['mode'] in (3, 4):
        keys.append('direction')
    if expected['mode'] not in (2, 3):
        keys += ['red', 'green', 'blue']
    if any(expected[key] != observed[key] for key in keys):
        raise RuntimeError('Firmware lighting readback differs from the requested effect.')


def retained_fan_command(status: dict, requested_profile: int) -> str:
    """Preserve cooling on ordinary mode changes; Quiet/Eco use Auto."""
    if requested_profile in (0, 6):
        return 'auto'
    modes = (status['cpu_mode'], status['gpu_mode'])
    if modes == (2, 2):
        return 'max'
    if modes == (3, 3):
        values = [status[f'{fan}_percent'] for fan in ('cpu', 'gpu')]
        if all(type(v) is int and 30 <= v <= 100 for v in values):
            return f'manual {values[0]} {values[1]}'
    if modes == (1, 1):
        return 'auto'
    raise ValueError('Cannot preserve the current fan setting. Select Automatic first.')


def write_control(target: str, value: str) -> dict:
    if target in OPTIONAL_CONTROLS:
        expected = validate_optional_command(target, value)
        (BASE / target).write_text(value + '\n')
        observed = read_optional(target)
        verify_optional(target, expected, observed)
        return {'optional_target': target, 'optional_value': observed}
    if target not in ('profile', 'fan_control'):
        raise ValueError('Unsupported hardware control.')
    if target == 'profile':
        requested = int(value)
        current = read_status()
        if requested not in [p[0] for p in PROFILES] or current['profile_mask'] is None or not current['profile_mask'] & (1 << requested):
            raise ValueError('This profile is not advertised by your firmware.')
        retained = retained_fan_command(current, requested)
    elif value not in ('auto', 'max'):
        parts = value.split()
        if len(parts) != 3 or parts[0] != 'manual' or any(not 30 <= int(p) <= 100 for p in parts[1:]):
            raise ValueError('Manual fan settings must be 30–100%.')
    (BASE / target).write_text(value + '\n')
    status = read_status()
    verify_change(status, target, value)
    if target == 'profile':
        # The kernel first returns cooling to Auto for a safe transition.
        # Restore the selected fan mode only after profile readback succeeds.
        if retained != 'auto':
            try:
                status = write_control('fan_control', retained)
                verify_change(status, 'profile', value)
            except Exception as error:
                try:
                    write_control('fan_control', 'auto')
                except Exception as restore_error:
                    raise RuntimeError('Fan restoration failed; automatic recovery could not be confirmed.') from restore_error
                raise RuntimeError('Performance mode changed, but the fan setting could not be restored. Cooling is Automatic.') from error
        else:
            verify_change(status, 'fan_control', 'auto')
    return status


def cpu_temperature():
    values = []
    for chip in Path('/sys/class/hwmon').glob('hwmon*'):
        if optional_text(chip / 'name') != 'coretemp':
            continue
        for item in chip.glob('temp*_input'):
            try:
                values.append(int(read_text(item)) / 1000)
            except (ValueError, OSError):
                pass
    return max(values) if values else None


def gpu_telemetry():
    try:
        process = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,temperature.gpu,utilization.gpu,clocks.gr,power.draw',
             '--format=csv,noheader,nounits'], capture_output=True, text=True,
            timeout=3, check=True)
        fields = next(csv.reader(io.StringIO(process.stdout)))
        return dict(zip(('name', 'temperature', 'load', 'clock', 'power'), (s.strip() for s in fields)))
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        return {}


def power_state():
    battery, ac = None, None
    for supply in Path('/sys/class/power_supply').glob('*'):
        kind = optional_text(supply / 'type')
        if kind == 'Battery':
            percent = optional_text(supply / 'capacity')
            state = optional_text(supply / 'status')
            if percent is not None:
                battery = f'{percent}% · {state or "Unknown"}'
        elif kind in ('Mains', 'USB', 'USB_C', 'USB_PD'):
            online = optional_text(supply / 'online')
            if online in ('0', '1'):
                ac = (ac is True) or online == '1'
    return battery, ac


def snapshot(include_optional=True):
    result = {'model': optional_text(Path('/sys/class/dmi/id/product_name')),
              'kernel': os.uname().release, 'driver_path': str(BASE), 'timestamp': time.time()}
    try:
        result['driver'] = read_status()
        result['driver_error'] = None
    except (OSError, ValueError) as error:
        result['driver'] = None
        result['driver_error'] = friendly_error(error)
    result['cpu_temperature'] = cpu_temperature()
    result['gpu'] = gpu_telemetry()
    result['battery'], result['ac_online'] = power_state()
    if include_optional:
        result['optional_errors'] = {}
        for target in OPTIONAL_CONTROLS:
            try:
                result[target] = read_optional(target)
            except (OSError, ValueError) as error:
                result[target] = None
                result['optional_errors'][target] = ('Driver support is not available.' if isinstance(error, FileNotFoundError) else friendly_error(error))
    return result


def friendly_error(error):
    if isinstance(error, PermissionError):
        return 'Access denied. Complete the one-time driver setup, then sign out and back in if group membership changed.'
    if isinstance(error, FileNotFoundError):
        return 'The Neo Control driver is not loaded. Run install.sh from this app’s folder to enable hardware controls.'
    return str(error)


class CoolingTarget45:
    """Aggressive, best-effort target. Ramp up immediately, down after 15s."""
    def __init__(self):
        self.duty = 100
        self.lower_since = None
        self.lower_request = None

    def choose(self, cpu, gpu, now, age=0):
        try:
            temperatures = [float(cpu), float(gpu)]
            valid = all(math.isfinite(t) and 0 <= t <= 130 for t in temperatures)
        except (TypeError, ValueError):
            valid = False
        if not valid or not 0 <= age <= 8:
            self.duty, self.lower_since, self.lower_request = 100, None, None
            return 'max', '45°C target · Sensor unavailable or stale · Maximum cooling'
        hottest = max(temperatures)
        desired = 100 if hottest >= 42 else 80 if hottest >= 39 else 60 if hottest >= 36 else 40
        if desired >= self.duty:
            self.duty, self.lower_since, self.lower_request = desired, None, None
        else:
            if self.lower_request != desired:
                self.lower_since, self.lower_request = now, desired
            if now - self.lower_since >= 15:
                self.duty, self.lower_since, self.lower_request = desired, None, None
        command = 'max' if self.duty == 100 else f'manual {self.duty} {self.duty}'
        state = 'Above target · Maximum cooling' if hottest >= 45 else f'{self.duty}% fan request'
        return command, f'45°C target · {hottest:.0f}°C hottest · {state}'


def apply_cooling_target(data, controller):
    """Only fresh sensor cycles renew a target lease; stalled cycles expire."""
    if not data.get('driver'):
        raise RuntimeError('Fan status unavailable; target lease will expire to Automatic.')
    command, message = controller.choose(data.get('cpu_temperature'), data.get('gpu', {}).get('temperature'),
                                         time.monotonic(), time.time() - data['timestamp'])
    try:
        verify_change(data['driver'], 'fan_control', command)
    except RuntimeError:
        data['driver'] = write_control('fan_control', command)
    (BASE / 'keepalive').write_text('1\n')
    data['cooling_target'] = message
    return data


def run_gui(smoke_test=False, screenshot_path=None, cool_target=False, screenshot_page='overview', resume_fans=False):
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, Gdk, Gio, GLib

    class NeoApplication(Gtk.Application):
        def __init__(self):
            super().__init__(application_id='io.local.NeoControl', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='neo-hardware')
            self.polling = False
            self.command_pending = False
            self.manual_owned = False
            self.cooling_target = None
            self.start_target_pending = cool_target
            self.resume_fans_pending = resume_fans
            self.closing = False
            self.latest = {}
            self.status = None
            self.buttons = []
            self.profile_buttons = {}
            self.fan_buttons = []
            self.optional_buttons = {}
            self.optional_initialized = set()
            self.optional_poll_time = 0
            self.screenshot_failed = False
            self.connect('activate', self.activate_window)
            self.connect('shutdown', self.on_shutdown)
            hide_action = Gio.SimpleAction.new('hide', None)
            hide_action.connect('activate', lambda *_: self.window.close())
            self.add_action(hide_action)
            quit_action = Gio.SimpleAction.new('quit', None)
            quit_action.connect('activate', lambda *_: self.quit_safely())
            self.add_action(quit_action)

        def label(self, text, css=None, wrap=False):
            label = Gtk.Label(label=text, xalign=0, wrap=wrap)
            if css:
                label.add_css_class(css)
            return label

        def box(self, spacing=12, horizontal=False):
            return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if horizontal else Gtk.Orientation.VERTICAL,
                           spacing=spacing)

        def card(self):
            box = self.box(16)
            box.add_css_class('card')
            return box

        def button(self, label, callback, css=None):
            button = Gtk.Button(label=label)
            button.connect('clicked', callback)
            if css:
                button.add_css_class(css)
            self.buttons.append(button)
            return button

        def gauge(self, title, key, maximum, unit):
            card = self.card()
            card.set_hexpand(True)
            card.append(self.label(title, 'eyebrow'))
            drawing = Gtk.DrawingArea(content_width=150, content_height=140)
            def draw(widget, cr, width, height):
                value = self.gauge_values.get(key)
                cx, cy, radius = width / 2, height / 2, min(width, height) / 2 - 18
                start, sweep = math.pi * .75, math.pi * 1.5
                cr.set_line_width(10)
                cr.set_source_rgb(.07, .19, .22)
                cr.arc(cx, cy, radius, start, start + sweep)
                cr.stroke()
                if value is not None:
                    cr.set_source_rgb(.05, .83, .9)
                    if unit == '°C' and value >= 85:
                        cr.set_source_rgb(1, .55, .25)
                    cr.arc(cx, cy, radius, start, start + sweep * min(max(value / maximum, 0), 1))
                    cr.stroke()
                cr.set_source_rgb(.92, .97, .98)
                cr.select_font_face('Sans', 0, 1)
                cr.set_font_size(32 if unit == '°C' else 28)
                text = '—' if value is None else f'{value:,.0f}'
                ext = cr.text_extents(text)
                cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + 6)
                cr.show_text(text)
                cr.set_font_size(12)
                cr.set_source_rgb(.49, .69, .73)
                ext = cr.text_extents(unit)
                cr.move_to(cx - ext.width / 2, cy + 29)
                cr.show_text(unit)
            drawing.set_draw_func(draw)
            card.append(drawing)
            detail = self.label('Reading live sensors…', 'muted', True)
            detail.set_halign(Gtk.Align.CENTER)
            card.append(detail)
            self.gauges[key] = drawing
            return card, detail

        def navigate(self, name):
            self.stack.set_visible_child_name(name)
            self.nav_buttons[name].grab_focus()
            for key, button in self.nav_buttons.items():
                if key == name:
                    button.add_css_class('selected')
                else:
                    button.remove_css_class('selected')

        def start_cooling_target(self, button=None):
            if self.command_pending or self.closing or not self.status:
                return
            self.cooling_target = CoolingTarget45()
            self.manual_owned = True
            self.target_state.set_text('Starting 45°C target · Checking temperatures…')
            self.message('45°C target enabled; cooling continues with the window closed. Temperatures may exceed the target under load.')
            self.poll()

        def activate_window(self, app):
            if hasattr(self, "window"):
                self.window.present()
                return
            Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', True)
            provider = Gtk.CssProvider()
            provider.load_from_data(b'''
                window { background: #060e13; color: #e9f5f7; }
                headerbar { background: #050b0f; box-shadow: none; border-bottom: 1px solid #123540; }
                .shell { background: #060e13; }
                .page { padding: 22px; }
                .brand { font-size: 20px; font-weight: 800; letter-spacing: 3px; color: #13d4e4; }
                .title { font-size: 28px; font-weight: 800; letter-spacing: 1px; }
                .subtitle, .muted { color: #8daab3; font-size: 12px; }
                .eyebrow { color: #38c9dc; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
                .section-title { font-size: 17px; font-weight: 700; }
                .card { background: #0b1b23; border: 1px solid #16404d; border-radius: 5px; padding: 18px; }
                .metric { font-size: 32px; font-weight: 700; }
                .sidebar { background: #081820; border-left: 1px solid #16404d; padding: 20px 16px; }
                .message { padding: 10px 16px; background: #10232b; color: #a9cbd3; font-size: 12px; }
                .error { color: #ffd49a; background: #362a1e; }
                .success { color: #6ce6d4; background: #0d2828; }
                button { background-image: none; background-color: #10252e; color: #c2d8df; border: 1px solid #24505e; min-height: 34px; border-radius: 3px; padding: 5px 13px; }
                button:hover { background-color: #163946; color: #fff; }
                button:disabled { opacity: 0.45; }
                button.primary, button.selected { background-color: #07586a; color: #fff; border-color: #13d4e4; }
                button.nav { background-color: transparent; border-color: transparent; border-radius: 0; min-height: 40px; }
                button.nav.selected { border-bottom: 2px solid #13d4e4; color: #13d4e4; }
                button.mode { min-height: 56px; }
                scale trough { min-height: 6px; background: #12323d; }
                scale highlight { background: #0dc8dd; }
                scale slider { background: #62e7ef; border-color: #0dc8dd; }
                dropdown button { min-height: 32px; }
                headerbar { min-height: 32px; padding: 0 6px; }
                headerbar button { min-height: 24px; min-width: 24px; padding: 2px; margin: 2px; background-color: transparent; border-color: transparent; border-radius: 4px; box-shadow: none; }
                headerbar button:hover { background-color: #163946; }
                headerbar windowcontrols button { min-height: 24px; min-width: 24px; padding: 2px; margin: 2px; }
                headerbar button image { min-height: 16px; min-width: 16px; -gtk-icon-size: 16px; }
                .small { font-size: 11px; }
            ''')
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.window = Gtk.ApplicationWindow(application=self, title='Neo Control · Helios Neo 16')
            self.window.set_default_size(1160, 820)
            self.window.connect('close-request', self.on_close)
            header = Gtk.HeaderBar()
            title = self.label('PHN16-71  /  Linux control center', 'muted')
            header.set_title_widget(title)
            menu = Gio.Menu()
            menu.append('Hide to background', 'app.hide')
            menu.append('Quit and restore Auto', 'app.quit')
            menu_button = Gtk.MenuButton(icon_name='open-menu-symbolic', tooltip_text='Application menu', menu_model=menu)
            header.pack_end(menu_button)
            diagnostics = Gtk.Button(icon_name='dialog-information-symbolic', tooltip_text='Copy hardware diagnostics')
            diagnostics.connect('clicked', self.show_diagnostics)
            header.pack_end(diagnostics)
            self.window.set_titlebar(header)
            shell = self.box(0)
            self.page = shell
            shell.add_css_class('shell')
            topbar = self.box(25, True)
            topbar.set_margin_start(22)
            topbar.set_margin_end(16)
            topbar.append(self.label('NEO CONTROL', 'brand'))
            nav = self.box(4, True)
            nav.set_hexpand(True)
            self.nav_buttons = {}
            for name, title in [('overview', 'Overview'), ('cooling', 'Fan Control'), ('lighting', 'Lighting'), ('battery', 'Battery')]:
                button = Gtk.Button(label=title)
                button.add_css_class('nav')
                button.connect('clicked', lambda _, key=name: self.navigate(key))
                nav.append(button)
                self.nav_buttons[name] = button
            topbar.append(nav)
            topbar.append(self.label('PHN16-71', 'eyebrow'))
            shell.append(topbar)
            body = self.box(0, True)
            body.set_vexpand(True)
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            self.stack.set_hexpand(True)
            self.stack.set_vexpand(True)
            self.stack.set_vhomogeneous(False)
            self.stack.set_hhomogeneous(False)
            pages = {}
            for name, title, description in [
                ('overview', 'HELIOS NEO 16', 'System overview / Live hardware telemetry'),
                ('cooling', 'FAN CONTROL', 'Choose your cooling mode and monitor both fans.'),
                ('lighting', 'KEYBOARD LIGHTING', 'Eight effects. Four zones. Your colors.'),
                ('battery', 'BATTERY CARE', 'Power status and charge protection.')]:
                content = self.box(16)
                content.add_css_class('page')
                content.append(self.label(title, 'title'))
                content.append(self.label(description, 'subtitle'))
                scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
                scroll.set_child(content)
                self.stack.add_named(scroll, name)
                pages[name] = content
            body.append(self.stack)
            sidebar = self.box(18)
            sidebar.add_css_class('sidebar')
            sidebar.set_size_request(205, -1)
            sidebar.append(self.label('LIVE MONITOR', 'eyebrow'))
            self.sidebar_values = {}
            for key, title in [('mode', 'SYSTEM MODE'), ('cooling', 'COOLING'), ('cpu', 'CPU'), ('gpu', 'GPU'), ('battery', 'POWER')]:
                block = self.box(6)
                block.append(self.label(title, 'muted'))
                label = self.label('Connecting…', None, True)
                label.set_max_width_chars(24)
                block.append(label)
                self.sidebar_values[key] = label
                sidebar.append(block)
            spacer = self.box()
            spacer.set_vexpand(True)
            sidebar.append(spacer)
            sidebar.append(self.label('LINUX EDITION', 'eyebrow'))
            sidebar.append(self.label('Independent software for\nPredator Helios Neo 16', 'muted', True))
            body.append(sidebar)
            shell.append(body)
            page = pages['overview']
            self.gauges, self.gauge_values = {}, {}
            metrics = self.box(16, True)
            self.metric_values = {}
            for key, title in [('cpu', 'CPU TEMPERATURE'), ('gpu', 'GPU TEMPERATURE')]:
                card, detail = self.gauge(title, key, 100, '°C')
                self.metric_values[key] = (None, detail)
                metrics.append(card)
            page.append(metrics)

            profiles = self.card()
            profiles.append(self.label('Performance mode', 'section-title'))
            self.profile_detail = self.label('Firmware performance profiles · GPU overclock offsets are not applied', 'muted', True)
            profiles.append(self.profile_detail)
            profiles.append(self.label('Balanced, Performance and Turbo retain fan settings. Quiet and Eco use firmware Auto.', 'muted', True))
            choices = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                                  max_children_per_line=5, min_children_per_line=1, homogeneous=True)
            for number, name, description in PROFILES:
                button = self.button(name, lambda b, n=number: self.command('profile', str(n)))
                button.set_tooltip_text(description)
                button.add_css_class('mode')
                choices.insert(button, -1)
                self.profile_buttons[number] = button
            profiles.append(choices)
            page.append(profiles)

            quick = self.box(12, True)
            quick.append(self.label('Auto, Maximum, custom speeds or a 45°C target.', 'muted', True))
            quick_button = Gtk.Button(label='Open Fan Control')
            quick_button.connect('clicked', lambda _: self.navigate('cooling'))
            quick.append(quick_button)
            page.append(quick)
            page = pages['cooling']
            fan_gauges = self.box(16, True)
            self.fan_details = {}
            for key, title in [('cpu_rpm', 'CPU FAN'), ('gpu_rpm', 'GPU FAN')]:
                card, detail = self.gauge(title, key, 8000, 'RPM')
                self.fan_details[key] = detail
                fan_gauges.append(card)
            page.append(fan_gauges)
            fans = self.card()
            fans.append(self.label('Cooling', 'section-title'))
            self.fan_state = self.label('Waiting for firmware fan telemetry', 'muted', True)
            fans.append(self.fan_state)
            row = self.box(10, True)
            for name, command in [('Auto', 'auto'), ('Max', 'max')]:
                button = self.button(name, lambda b, c=command: self.command('fan_control', c))
                button.set_hexpand(True)
                button.add_css_class('mode')
                row.append(button)
                self.fan_buttons.append(button)
            self.target_button = self.button('45°C target', self.start_cooling_target)
            self.target_button.add_css_class('mode')
            self.target_button.set_hexpand(True)
            row.append(self.target_button)
            self.custom_button = Gtk.Button(label='Custom')
            self.custom_button.add_css_class('mode')
            self.custom_button.set_hexpand(True)
            self.custom_button.connect('clicked', lambda _: self.manual_panel.set_visible(not self.manual_panel.get_visible()))
            row.append(self.custom_button)
            fans.append(row)
            self.target_state = self.label('45°C target is off · Firmware Auto follows Acer’s own fan curve.', 'muted', True)
            fans.append(self.target_state)
            fans.append(self.label('45°C is a cooling target, not a temperature limit. It uses maximum fans from 42°C and continues in the background. Selecting another fan or performance mode stops the target.', 'muted', True))
            self.manual_panel = self.box(12)
            self.manual_panel.set_visible(False)
            self.sliders = []
            self.fan_labels = []
            for name in ('CPU fan', 'GPU fan'):
                fanrow = self.box(12, True)
                fanrow.append(self.label(name))
                scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 30, 100, 1)
                scale.set_value(60)
                scale.set_draw_value(False)
                scale.set_hexpand(True)
                percentage = self.label('60%')
                scale.connect('value-changed', lambda s, l=percentage: l.set_text(f'{int(s.get_value())}%'))
                fanrow.append(scale)
                fanrow.append(percentage)
                self.manual_panel.append(fanrow)
                self.sliders.append(scale)
            self.manual_button = self.button('Apply manual speeds', self.apply_manual, 'primary')
            self.manual_panel.append(self.manual_button)
            fans.append(self.manual_panel)
            fans.append(self.label('Closing the window keeps cooling running in the background. Press the PredatorSense button to reopen. Use the menu’s Quit and restore Auto to stop. If the app stops responding, the driver restores Auto.', 'muted', True))
            page.append(fans)
            page = pages['battery']
            battery_card = self.card()
            battery_value = self.label('—', 'metric')
            battery_detail = self.label('Reading…', 'muted', True)
            battery_card.append(battery_value)
            battery_card.append(battery_detail)
            self.metric_values['battery'] = (battery_value, battery_detail)
            battery_card.append(self.label('Battery care', 'section-title'))
            self.battery_limit_state = self.label('Checking charge-limit support…', 'muted', True)
            battery_card.append(self.battery_limit_state)
            row = self.box(10, True)
            self.optional_buttons['battery_limit'] = []
            for label, value in [('Limit charging to 80%', '1'), ('Allow full charging', '0')]:
                button = self.button(label, lambda b, v=value: self.command('battery_limit', v))
                row.append(button)
                self.optional_buttons['battery_limit'].append(button)
            battery_card.append(row)
            page.append(battery_card)

            page = pages['lighting']
            lighting = self.card()
            lighting.append(self.label('Keyboard lighting', 'section-title'))
            self.lighting_state = self.label('Checking lighting support…', 'muted', True)
            lighting.append(self.lighting_state)
            row = self.box(12, True)
            self.effect = Gtk.DropDown.new_from_strings(LIGHTING_MODES)
            self.effect.set_hexpand(True)
            row.append(self.effect)
            self.effect_color = Gtk.ColorDialogButton.new(Gtk.ColorDialog.new())
            self.effect_color.set_tooltip_text('Effect color')
            self.set_color(self.effect_color, '55DCCA')
            row.append(self.effect_color)
            self.direction = Gtk.DropDown.new_from_strings(['Direction 1', 'Direction 2'])
            row.append(self.direction)
            lighting.append(row)
            self.effect_speed = self.setting_slider(lighting, 'Effect speed', 1, 9, 4)
            self.effect_brightness = self.setting_slider(lighting, 'Brightness', 0, 100, 70)
            self.effect.connect('notify::selected', lambda *_: self.update_effect_fields())
            button = self.button('Apply lighting effect', self.apply_lighting)
            lighting.append(button)
            self.optional_buttons['lighting'] = [button]
            lighting.append(self.label('Neon and Wave use their own colors. Some effects ignore speed or direction. Firmware readback confirms the setting; check the keyboard to confirm its appearance.', 'muted', True))
            page.append(lighting)

            zones = self.card()
            zones.append(self.label('Four-zone colors', 'section-title'))
            self.zones_state = self.label('Checking per-zone support…', 'muted', True)
            zones.append(self.zones_state)
            row = self.box(16, True)
            self.zone_colors = []
            for index, color in enumerate(('55DCCA', '55AFFF', 'AD78FF', 'FF7298')):
                column = self.box(6)
                column.set_hexpand(True)
                column.append(self.label(f'Zone {index + 1}', 'muted'))
                color_button = Gtk.ColorDialogButton.new(Gtk.ColorDialog.new())
                self.set_color(color_button, color)
                column.append(color_button)
                row.append(column)
                self.zone_colors.append(color_button)
            zones.append(row)
            self.zone_brightness = self.setting_slider(zones, 'Brightness', 0, 100, 70)
            button = self.button('Apply four-zone colors', self.apply_zones)
            zones.append(button)
            self.optional_buttons['zones'] = [button]
            page.append(zones)
            footer = self.box(6)
            self.readback = self.label('Waiting for firmware response…', 'muted', True)
            self.readback.set_margin_start(16)
            self.readback.set_margin_end(16)
            footer.append(self.readback)
            self.banner = self.label('Connecting to your hardware…', 'message', True)
            footer.append(self.banner)
            shell.append(footer)
            self.window.set_child(shell)
            self.navigate(screenshot_page if screenshot_path else ('cooling' if cool_target else 'overview'))
            self.enable_controls(False)
            self.window.present()
            GLib.idle_add(lambda: (self.nav_buttons[self.stack.get_visible_child_name()].grab_focus(), False)[1])
            self.poll()
            GLib.timeout_add_seconds(2, self.poll)
            GLib.timeout_add_seconds(10, self.keepalive)
            if screenshot_path:
                GLib.timeout_add_seconds(5, self.save_screenshot)
            elif smoke_test:
                GLib.timeout_add_seconds(5, self.verify_ui)

        def verify_ui(self):
            try:
                for name, button in self.nav_buttons.items():
                    button.emit('clicked')
                    if self.stack.get_visible_child_name() != name:
                        raise RuntimeError(f'Navigation failed: {name}')
                self.navigate('cooling')
                before = self.manual_panel.get_visible()
                self.custom_button.emit('clicked')
                if self.manual_panel.get_visible() == before:
                    raise RuntimeError('Custom fan controls did not open.')
                self.custom_button.emit('clicked')
                if self.manual_panel.get_visible() != before:
                    raise RuntimeError('Custom fan controls did not close.')
                print('GTK checks passed: four navigation tabs and Custom controls; no settings changed.')
            except Exception as error:
                self.screenshot_failed = True
                print(f'GTK check failed: {error}', file=sys.stderr)
            self.quit_safely()
            return False

        def save_screenshot(self):
            if self.stack.get_visible_child_name() != screenshot_page:
                self.navigate(screenshot_page)
                GLib.timeout_add(350, self.save_screenshot)
                return False
            try:
                paintable = Gtk.WidgetPaintable.new(self.page)
                snapshot = Gtk.Snapshot.new()
                paintable.snapshot(snapshot, self.page.get_width(), self.page.get_height())
                node = snapshot.to_node()
                if node is None:
                    raise RuntimeError('The window has not rendered yet.')
                texture = self.window.get_renderer().render_texture(node, None)
                if not texture.save_to_png(screenshot_path):
                    raise RuntimeError('Could not save the rendered image.')
                print(f'Rendered app saved to {screenshot_path}')
            except Exception as error:
                self.screenshot_failed = True
                print(f'Screenshot failed: {error}', file=sys.stderr)
            self.quit_safely()
            return False

        def setting_slider(self, container, name, lower, upper, initial):
            row = self.box(12, True)
            row.append(self.label(name))
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lower, upper, 1)
            scale.set_value(initial)
            scale.set_hexpand(True)
            scale.set_draw_value(True)
            row.append(scale)
            container.append(row)
            return scale

        def set_color(self, button, value):
            rgba = Gdk.RGBA()
            rgba.parse('#' + value)
            button.set_rgba(rgba)

        def color_hex(self, button):
            rgba = button.get_rgba()
            return ''.join(f'{round(channel * 255):02X}' for channel in (rgba.red, rgba.green, rgba.blue))

        def update_effect_fields(self):
            enabled = self.latest.get('lighting') is not None and not self.command_pending and not self.closing
            mode = self.effect.get_selected()
            self.effect_color.set_sensitive(enabled and mode not in (2, 3))
            self.direction.set_sensitive(enabled and mode in (3, 4))
            self.effect_speed.set_sensitive(enabled and mode != 0)

        def apply_lighting(self, button):
            color = self.color_hex(self.effect_color)
            components = [int(color[index:index+2], 16) for index in (0, 2, 4)]
            values = [self.effect.get_selected(), int(self.effect_speed.get_value()),
                      int(self.effect_brightness.get_value()), self.direction.get_selected() + 1] + components
            self.command('lighting', ' '.join(map(str, values)))

        def apply_zones(self, button):
            self.command('zones', ' '.join(self.color_hex(color) for color in self.zone_colors) +
                         f' {int(self.zone_brightness.get_value())}')

        def render_optional(self):
            errors = self.latest.get('optional_errors', {})
            limit = self.latest.get('battery_limit')
            self.battery_limit_state.set_text(('Charge limit: 80%' if limit == 1 else 'Full charging allowed') if limit is not None else errors.get('battery_limit', 'Charge-limit support unavailable.'))
            effect = self.latest.get('lighting')
            if effect is not None:
                self.lighting_state.set_text(f"Firmware effect: {LIGHTING_MODES[effect['mode']]} · Brightness {effect['brightness']}%")
                if 'lighting' not in self.optional_initialized:
                    self.effect.set_selected(effect['mode'])
                    self.effect_speed.set_value(effect['speed'])
                    self.effect_brightness.set_value(effect['brightness'])
                    self.direction.set_selected(max(0, effect['direction'] - 1))
                    self.set_color(self.effect_color, ''.join(f"{effect[key]:02X}" for key in ('red', 'green', 'blue')))
                    self.optional_initialized.add('lighting')
            else:
                self.lighting_state.set_text(errors.get('lighting', 'Lighting support unavailable.'))
            zones = self.latest.get('zones')
            if zones is not None:
                self.zones_state.set_text(f"Firmware colors: {' · '.join(zones['colors'])} · Brightness {zones['brightness']}%")
                if 'zones' not in self.optional_initialized:
                    for button, color in zip(self.zone_colors, zones['colors']):
                        self.set_color(button, color)
                    self.zone_brightness.set_value(zones['brightness'])
                    self.optional_initialized.add('zones')
            else:
                self.zones_state.set_text(errors.get('zones', 'Per-zone support unavailable.'))

        def message(self, text, kind=None):
            self.banner.set_text(text)
            for css in ('error', 'success'):
                self.banner.remove_css_class(css)
            if kind:
                self.banner.add_css_class(kind)

        def enable_controls(self, enabled):
            for button in self.buttons:
                button.set_sensitive(enabled and not self.command_pending)
            for slider in self.sliders:
                slider.set_sensitive(enabled and not self.command_pending)
            if self.status:
                mask = self.status['profile_mask']
                for number, button in self.profile_buttons.items():
                    supported = mask is not None and bool(mask & (1 << number))
                    button.set_visible(supported)
                    ac_connected = self.latest.get('ac_online') is True
                    button.set_sensitive(enabled and not self.command_pending and supported and
                                         (ac_connected or number in (1, 6)))
                fan_modes_available = all(self.status[f'{fan}_mode'] is not None for fan in ('cpu', 'gpu'))
                for button in self.fan_buttons:
                    button.set_sensitive(enabled and not self.command_pending and fan_modes_available)
                self.target_button.set_sensitive(enabled and not self.command_pending and fan_modes_available)
                self.custom_button.set_sensitive(enabled and not self.command_pending and fan_modes_available)
                manual_available = fan_modes_available and all(self.status[f'{fan}_percent'] is not None for fan in ('cpu', 'gpu'))
                self.manual_button.set_sensitive(enabled and not self.command_pending and manual_available)
                for slider in self.sliders:
                    slider.set_sensitive(enabled and not self.command_pending and manual_available)
            for target, buttons in self.optional_buttons.items():
                available = self.latest.get(target) is not None and not self.command_pending and not self.closing
                for button in buttons:
                    button.set_sensitive(available)
            lighting_available = self.latest.get('lighting') is not None and not self.command_pending and not self.closing
            for widget in (self.effect, self.effect_color, self.direction, self.effect_speed, self.effect_brightness):
                widget.set_sensitive(lighting_available)
            self.update_effect_fields()
            zones_available = self.latest.get('zones') is not None and not self.command_pending and not self.closing
            for widget in [*self.zone_colors, self.zone_brightness]:
                widget.set_sensitive(zones_available)

        def submit(self, function, callback):
            future = self.pool.submit(function)
            def completed(future):
                try:
                    result, error = future.result(), None
                except Exception as error_value:
                    result, error = None, error_value
                GLib.idle_add(callback, result, error)
            future.add_done_callback(completed)

        def poll(self):
            if not self.closing and not self.polling and not self.command_pending:
                self.polling = True
                include_optional = time.monotonic() - self.optional_poll_time > 6
                if include_optional:
                    self.optional_poll_time = time.monotonic()
                target = self.cooling_target
                def collect():
                    data = snapshot(include_optional)
                    if target is not None and self.cooling_target is target and not self.closing:
                        try:
                            apply_cooling_target(data, target)
                        except Exception as error:
                            data['target_error'] = friendly_error(error)
                            try:
                                data['driver'] = write_control('fan_control', 'auto')
                            except Exception as recovery:
                                data['target_error'] += ' Auto recovery unconfirmed: ' + friendly_error(recovery)
                    return data
                self.submit(collect, self.update_snapshot)
            return not self.closing

        def update_snapshot(self, data, error):
            self.polling = False
            if self.closing:
                return False
            if error:
                self.message(f'Hardware status unavailable: {friendly_error(error)}', 'error')
                self.status = None
                self.enable_controls(False)
                return False
            self.latest.update(data)
            if 'optional_errors' in data:
                self.render_optional()
            temperature = data['cpu_temperature']
            self.gauge_values['cpu'] = temperature
            self.metric_values['cpu'][1].set_text('Hottest reported core' if temperature is not None else 'Sensor unavailable')
            gpu = data['gpu']
            self.gauge_values['gpu'] = float(gpu['temperature']) if gpu.get('temperature', '').isdigit() else None
            self.metric_values['gpu'][1].set_text(f"{gpu.get('load', '—')}% load · {gpu.get('clock', '—')} MHz" if gpu else 'NVIDIA telemetry unavailable')
            battery = data['battery']
            self.metric_values['battery'][0].set_text(battery.split(' · ')[0] if battery else '—')
            self.metric_values['battery'][1].set_text(('AC connected' if data['ac_online'] else 'On battery') if data['ac_online'] is not None else 'Power source unavailable')
            self.sidebar_values['cpu'].set_text(f'{temperature:.0f}°C' if temperature is not None else 'Unavailable')
            self.sidebar_values['gpu'].set_text(f"{gpu.get('temperature', '—')}°C · {gpu.get('clock', '—')} MHz")
            self.sidebar_values['battery'].set_text((battery or 'Unavailable') + ('\nAC connected' if data['ac_online'] else '\nOn battery'))
            if data.get('target_error') and self.cooling_target is not None:
                self.cooling_target = None
                self.target_state.set_text('45°C target stopped · ' + data['target_error'])
                self.message('Cooling target stopped: ' + data['target_error'], 'error')
            elif self.cooling_target is not None and data.get('cooling_target'):
                self.target_state.set_text(data['cooling_target'])
            previous = self.status
            self.status = data['driver']
            if self.status:
                self.render_status()
                if previous is None:
                    self.message('Connected · Firmware controls are available.', 'success')
            else:
                self.message(data['driver_error'], 'error')
                self.fan_state.set_text('Fan state unavailable — hardware controls disabled')
                self.readback.set_text('Driver setup is required; temperature and battery monitoring still work.')
                for fan in ('cpu', 'gpu'):
                    self.gauge_values[fan + '_rpm'] = None
                    self.fan_details[fan + '_rpm'].set_text('Unavailable')
            for drawing in self.gauges.values():
                drawing.queue_draw()
            self.enable_controls(self.status is not None)
            if self.resume_fans_pending and self.status:
                self.resume_fans_pending = False
                self.manual_owned = any(self.status[f'{fan}_mode'] in (2, 3) for fan in ('cpu', 'gpu'))
                if self.manual_owned:
                    self.keepalive()
            if self.start_target_pending and self.status:
                self.start_target_pending = False
                self.start_cooling_target()
            return False

        def render_status(self):
            status = self.status
            def fan_text(fan):
                rpm = status[f'{fan}_rpm']
                rpm_text = f'{rpm:,} RPM' if rpm is not None else 'RPM unavailable'
                return f"{fan.upper()}  {rpm_text} · {MODE_NAMES.get(status[f'{fan}_mode'], 'Mode unavailable')}"
            self.fan_state.set_text(f"{fan_text('cpu')}     /     {fan_text('gpu')}")
            for fan in ('cpu', 'gpu'):
                self.gauge_values[fan + '_rpm'] = status[fan + '_rpm']
                self.fan_details[fan + '_rpm'].set_text(MODE_NAMES.get(status[fan + '_mode'], 'Unavailable'))
            for drawing in self.gauges.values():
                drawing.queue_draw()
            for mode, button in enumerate(self.fan_buttons, 1):
                if self.cooling_target is None and status['cpu_mode'] == status['gpu_mode'] == mode:
                    button.add_css_class('selected')
                else:
                    button.remove_css_class('selected')
            if self.cooling_target is not None:
                self.target_button.add_css_class('selected')
            else:
                self.target_button.remove_css_class('selected')
            if self.cooling_target is None and status['cpu_mode'] == status['gpu_mode'] == 3:
                self.custom_button.add_css_class('selected')
            else:
                self.custom_button.remove_css_class('selected')
            for number, button in self.profile_buttons.items():
                if status['profile'] == number:
                    button.add_css_class('selected')
                else:
                    button.remove_css_class('selected')
            name = dict((number, name) for number, name, _ in PROFILES).get(status['profile'], 'Profile unavailable')
            self.sidebar_values['mode'].set_text(name)
            self.sidebar_values['cooling'].set_text('45°C target' if self.cooling_target is not None else MODE_NAMES.get(status['cpu_mode'], 'Unavailable'))
            manual_targets = []
            for fan in ('cpu', 'gpu'):
                if status[f'{fan}_mode'] == 3 and status[f'{fan}_percent'] is not None:
                    manual_targets.append(f"{fan.upper()} manual target {status[f'{fan}_percent']}%")
            suffix = ' · '.join(manual_targets) if manual_targets else 'Live cooling state shown above'
            self.readback.set_text(f"Firmware readback: {name} · {suffix}")

        def apply_manual(self, button):
            values = [int(slider.get_value()) for slider in self.sliders]
            self.command('fan_control', f'manual {values[0]} {values[1]}')

        def command(self, target, value):
            if self.command_pending or self.closing or (not self.status and target not in OPTIONAL_CONTROLS):
                return
            stopped_target = self.cooling_target is not None and target in ('fan_control', 'profile')
            if target in ('fan_control', 'profile'):
                self.cooling_target = None
                self.start_target_pending = False
                self.target_state.set_text('45°C target is off · Choose 45°C target to enable it again.')
            self.command_pending = True
            self.enable_controls(False)
            self.message('Applying and checking the firmware response…')
            # Own a manual lease before dispatch: even if readback fails after a
            # write, explicitly quitting still tries to return the fans to auto.
            if target == 'fan_control' and value != 'auto':
                self.manual_owned = True
            if target == 'profile' and int(value) not in (0, 6):
                self.manual_owned = True
            def finished(result, error):
                self.command_pending = False
                if self.closing:
                    return False
                if error:
                    self.message(f'Change could not be verified: {friendly_error(error)}', 'error')
                else:
                    if target in OPTIONAL_CONTROLS:
                        self.latest[target] = result['optional_value']
                        self.render_optional()
                    else:
                        self.status = result
                    if target == 'fan_control' and value == 'auto':
                        self.manual_owned = False
                    if target == 'profile':
                        self.manual_owned = any(result[f'{fan}_mode'] in (2, 3) for fan in ('cpu', 'gpu'))
                    if self.status:
                        self.render_status()
                    self.message('Applied · Firmware readback confirmed. Check the keyboard appearance.' if target in ('lighting', 'zones') else 'Applied · Confirmed by firmware readback.', 'success')
                self.enable_controls(self.status is not None)
                return False
            def apply():
                if stopped_target and target == 'profile':
                    write_control('fan_control', 'auto')
                return write_control(target, value)
            self.submit(apply, finished)

        def keepalive(self):
            if self.closing:
                return False
            if self.manual_owned and self.cooling_target is None:
                def heartbeat():
                    (BASE / 'keepalive').write_text('1\n')
                def finished(result, error):
                    if error and not self.closing:
                        self.message('Fan lease could not be renewed. The driver will return to automatic cooling.', 'error')
                    return False
                self.submit(heartbeat, finished)
            return True

        def show_diagnostics(self, button):
            report = json.dumps(self.latest, indent=2)
            dialog = Gtk.Window(title='Neo Control diagnostics', transient_for=self.window, modal=True)
            dialog.set_default_size(660, 500)
            layout = self.box(12)
            layout.set_margin_top(16)
            layout.set_margin_bottom(16)
            layout.set_margin_start(16)
            layout.set_margin_end(16)
            view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text(report)
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_child(view)
            layout.append(scroll)
            copy = Gtk.Button(label='Copy diagnostics')
            copy.connect('clicked', lambda _: Gdk.Display.get_default().get_clipboard().set(report))
            layout.append(copy)
            dialog.set_child(layout)
            dialog.present()

        def on_close(self, window):
            if not self.closing:
                window.set_visible(False)
            return True

        def quit_safely(self):
            if self.closing:
                return True
            self.closing = True
            self.enable_controls(False)
            if self.manual_owned:
                self.message('Returning fans to automatic control…')
                def finished(result, error):
                    if error:
                        print('Could not restore automatic fan mode; kernel lease timeout remains active:', friendly_error(error), file=sys.stderr)
                    self.window.destroy()
                    self.quit()
                    return False
                self.submit(lambda: write_control('fan_control', 'auto'), finished)
            else:
                self.window.destroy()
                self.quit()
            return True

        def on_shutdown(self, app):
            self.pool.shutdown(wait=False, cancel_futures=True)

    app = NeoApplication()
    result = app.run([])
    return 1 if app.screenshot_failed else result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnose', action='store_true', help='Print live read-only diagnostics without opening GTK')
    parser.add_argument('--smoke-test', action='store_true', help='Open the UI briefly without applying settings')
    parser.add_argument('--cool-target', action='store_true', help='Start the best-effort 45°C cooling target')
    parser.add_argument('--resume-fans', action='store_true', help='Continue the existing manual/maximum fan lease after an app restart')
    parser.add_argument('--screenshot-page', choices=('overview', 'cooling', 'lighting', 'battery'), default='overview')
    parser.add_argument('--screenshot', metavar='PATH', help='Render this app’s own content to a PNG and exit without changing settings')
    args = parser.parse_args()
    if args.smoke_test and (args.cool_target or args.resume_fans):
        parser.error('--smoke-test cannot be combined with fan-control startup options')
    if args.cool_target and args.resume_fans:
        parser.error('Choose either --cool-target or --resume-fans')
    if args.diagnose:
        print(json.dumps(snapshot(), indent=2))
        return 0
    return run_gui(args.smoke_test, args.screenshot, args.cool_target, args.screenshot_page, args.resume_fans)


if __name__ == '__main__':
    raise SystemExit(main())
