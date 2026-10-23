#!/usr/bin/python3
"""Validation checks for untrusted/failed driver replies and firmware refusal."""
import json
import unittest
from unittest.mock import patch, MagicMock
import neo_control as neo

STATUS = {'profile': 1, 'profile_mask': 115, 'cpu_rpm': 2100, 'gpu_rpm': 2200,
          'cpu_mode': 1, 'gpu_mode': 1, 'cpu_percent': 50, 'gpu_percent': 50}


class FirmwareReadbackTests(unittest.TestCase):
    def test_failed_or_partial_reads_cannot_be_presented_as_hardware_success(self):
        for raw in ('null', '{}', '{"profile":1}', '{bad json', json.dumps({**STATUS, 'cpu_rpm': -1}),
                    json.dumps({**STATUS, 'gpu_mode': True}), json.dumps({**STATUS, 'cpu_percent': 255})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                neo.parse_status(raw)

    def test_unavailable_telemetry_preserves_valid_control_capabilities(self):
        partial = {**STATUS, 'cpu_rpm': None, 'gpu_percent': None}
        observed = neo.parse_status(json.dumps(partial))
        self.assertIsNone(observed['cpu_rpm'])
        neo.verify_change(observed, 'fan_control', 'auto')
        neo.verify_change(observed, 'profile', '1')

    def test_missing_manual_readback_never_counts_as_success(self):
        partial = {**STATUS, 'cpu_mode': 3, 'gpu_mode': 3, 'gpu_percent': None}
        with self.assertRaises(RuntimeError):
            neo.verify_change(partial, 'fan_control', 'manual 50 50')

    def test_firmware_profile_refusal_is_detected(self):
        with self.assertRaises(RuntimeError):
            neo.verify_change(STATUS, 'profile', '5')

    def test_partial_fan_mode_change_is_not_success(self):
        partial = {**STATUS, 'cpu_mode': 2, 'gpu_mode': 1}
        with self.assertRaises(RuntimeError):
            neo.verify_change(partial, 'fan_control', 'max')

    def test_manual_speed_refusal_is_detected_even_with_correct_mode(self):
        partial = {**STATUS, 'cpu_mode': 3, 'gpu_mode': 3, 'cpu_percent': 70, 'gpu_percent': 50}
        with self.assertRaises(RuntimeError):
            neo.verify_change(partial, 'fan_control', 'manual 70 70')

    def test_integer_quantization_does_not_report_false_failure(self):
        rounded = {**STATUS, 'cpu_mode': 3, 'gpu_mode': 3, 'cpu_percent': 69, 'gpu_percent': 71}
        neo.verify_change(rounded, 'fan_control', 'manual 70 70')


class ProfileCoolingTests(unittest.TestCase):
    def test_regular_modes_retain_maximum_and_manual(self):
        for profile in (1, 4, 5):
            self.assertEqual(neo.retained_fan_command({**STATUS, 'cpu_mode': 2, 'gpu_mode': 2}, profile), 'max')
            self.assertEqual(neo.retained_fan_command({**STATUS, 'cpu_mode': 3, 'gpu_mode': 3}, profile), 'manual 50 50')

    def test_quiet_and_eco_clear_override(self):
        for profile in (0, 6):
            self.assertEqual(neo.retained_fan_command({**STATUS, 'cpu_mode': 2, 'gpu_mode': 2}, profile), 'auto')

    def test_unknown_manual_target_is_not_replayed(self):
        with self.assertRaises(ValueError):
            neo.retained_fan_command({**STATUS, 'cpu_mode': 3, 'gpu_mode': 3, 'cpu_percent': None}, 5)

    def test_profile_readback_precedes_fan_restore(self):
        initial = {**STATUS, 'cpu_mode': 2, 'gpu_mode': 2}
        final = {**initial, 'profile': 5}
        base = MagicMock()
        with patch.object(neo, 'BASE', base), patch.object(neo, 'read_status', side_effect=[initial, {**STATUS, 'profile': 5}, final]):
            self.assertEqual(neo.write_control('profile', '5'), final)
        self.assertEqual([c.args[0] for c in base.__truediv__.call_args_list], ['profile', 'fan_control'])

    def test_failed_restore_returns_to_auto_and_reports_partial_change(self):
        initial = {**STATUS, 'cpu_mode': 2, 'gpu_mode': 2}
        automatic = {**STATUS, 'profile': 5}
        base = MagicMock()
        with patch.object(neo, 'BASE', base), patch.object(neo, 'read_status', side_effect=[initial, automatic, automatic, automatic]):
            with self.assertRaisesRegex(RuntimeError, 'Cooling is Automatic'):
                neo.write_control('profile', '5')
        self.assertEqual([c.args[0] for c in base.__truediv__.return_value.write_text.call_args_list], ['5\n', 'max\n', 'auto\n'])


class CoolingTargetTests(unittest.TestCase):
    def test_hot_either_sensor_immediately_requests_maximum(self):
        for cpu, gpu in [(42, 30), (30, 42), (95, 85)]:
            target = neo.CoolingTarget45()
            target.duty = 40
            self.assertEqual(target.choose(cpu, gpu, 0)[0], 'max')

    def test_missing_invalid_or_stale_sensors_cannot_reduce_cooling(self):
        for cpu, gpu, age in [(None, 30, 0), (30, 'N/A', 0), (float('nan'), 30, 0),
                              (-1, 30, 0), (30, 30, 9), (30, 30, -1)]:
            target = neo.CoolingTarget45()
            target.duty = 40
            self.assertEqual(target.choose(cpu, gpu, 0, age)[0], 'max')

    def test_cooling_must_stabilize_before_ramp_down(self):
        target = neo.CoolingTarget45()
        self.assertEqual(target.choose(35, 35, 0)[0], 'max')
        self.assertEqual(target.choose(35, 35, 14)[0], 'max')
        self.assertEqual(target.choose(35, 35, 15)[0], 'manual 40 40')
        self.assertEqual(target.choose(40, 35, 16)[0], 'manual 80 80')
        self.assertEqual(target.choose(42, 35, 17)[0], 'max')

    def test_temperature_fluctuation_resets_lowering_timer(self):
        target = neo.CoolingTarget45()
        target.choose(35, 35, 0)
        target.choose(40, 35, 14)
        self.assertEqual(target.choose(35, 35, 16)[0], 'max')
        self.assertEqual(target.choose(35, 35, 30)[0], 'max')
        self.assertEqual(target.choose(35, 35, 31)[0], 'manual 40 40')

    def test_unknown_driver_does_not_renew_lease(self):
        base = MagicMock()
        with patch.object(neo, 'BASE', base), self.assertRaises(RuntimeError):
            neo.apply_cooling_target({'driver': None}, neo.CoolingTarget45())
        base.__truediv__.assert_not_called()

    def test_external_auto_change_is_corrected_on_next_fresh_cycle(self):
        data = {'driver': STATUS, 'cpu_temperature': 50, 'gpu': {'temperature': '40'}, 'timestamp': 100}
        maximum = {**STATUS, 'cpu_mode': 2, 'gpu_mode': 2}
        with patch.object(neo, 'BASE'), patch.object(neo.time, 'time', return_value=100), patch.object(neo, 'write_control', return_value=maximum) as write:
            neo.apply_cooling_target(data, neo.CoolingTarget45())
        write.assert_called_once_with('fan_control', 'max')


class OptionalControlTests(unittest.TestCase):
    def test_invalid_effect_never_reaches_hardware_write(self):
        for command in ('8 0 50 1 0 0 0', '1 10 50 1 0 0 0', '1 1 101 1 0 0 0',
                        '1 1 50 0 0 0 0', '1 1 50 1 0 -1 0', '1 1 50 1 0 0'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                neo.validate_optional_command('lighting', command)

    def test_zone_color_validation_is_exact(self):
        for command in ('ABCDEF ABCDEF ABCDEF 70', 'ABCDEF ABCDEF ABCDEF ABCDEG 70',
                        'ABCDEF ABCDEF ABCDEF ABCDEF 101'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                neo.validate_optional_command('zones', command)

    def test_wave_verification_ignores_only_its_builtin_colors(self):
        expected = neo.validate_optional_command('lighting', '3 4 70 1 255 128 64')
        observed = {**expected, 'red': 0, 'green': 0, 'blue': 0}
        neo.verify_optional('lighting', expected, observed)

    def test_static_verification_ignores_speed_and_direction(self):
        expected = neo.validate_optional_command('lighting', '0 4 70 1 255 128 64')
        observed = {**expected, 'speed': 0, 'direction': 0}
        neo.verify_optional('lighting', expected, observed)

    def test_active_effect_speed_and_direction_mismatches_fail(self):
        for mode, key, changed in [(1, 'speed', 0), (2, 'speed', 0), (3, 'speed', 0),
                                   (3, 'direction', 2), (4, 'direction', 2), (7, 'speed', 0)]:
            expected = neo.validate_optional_command('lighting', f'{mode} 4 70 1 255 128 64')
            with self.subTest(mode=mode, key=key), self.assertRaises(RuntimeError):
                neo.verify_optional('lighting', expected, {**expected, key: changed})

    def test_effect_mode_mismatch_is_not_success(self):
        expected = neo.validate_optional_command('lighting', '1 4 70 1 255 128 64')
        with self.assertRaises(RuntimeError):
            neo.verify_optional('lighting', expected, {**expected, 'mode': 0})

    def test_charge_limit_refusal_is_not_success(self):
        with self.assertRaises(RuntimeError):
            neo.verify_optional('battery_limit', 1, 0)


if __name__ == '__main__':
    unittest.main()
