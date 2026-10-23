"""Installer decisions tested without package installation or firmware writes."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import setup_support as setup


class DependencyTests(unittest.TestCase):
    def test_cachyos_variants_use_exact_kernel_headers_and_full_upgrade(self):
        for pkg in ('linux-cachyos', 'linux-cachyos-lts', 'linux-cachyos-bmq', 'linux-cachyos-lto'):
            command = setup.dependency_command({'ID': 'cachyos', 'ID_LIKE': 'arch'}, '7.2-test', pkg)
            self.assertEqual(command[:3], ['pacman', '-Syu', '--needed'])
            self.assertIn(pkg + '-headers', command)
            self.assertIn('python-cairo', command)
            for tool in ('clang', 'llvm', 'lld'):
                self.assertIn(tool, command)
            if pkg != 'linux-cachyos':
                self.assertNotIn('linux-cachyos-headers', command)

    def test_arch_kernel_variants(self):
        for pkg in ('linux', 'linux-lts', 'linux-zen'):
            self.assertIn(pkg + '-headers', setup.dependency_command({'ID': 'arch'}, 'test', pkg))

    def test_unknown_or_unsafe_package_names_fail_without_guessing(self):
        for pkg in (None, '', '--help', 'linux;reboot', 'linux x', '../../linux'):
            with self.subTest(pkg=pkg), self.assertRaises(ValueError):
                setup.dependency_command({'ID': 'cachyos'}, 'test', pkg)

    def test_fedora_targets_running_kernel(self):
        cmd = setup.dependency_command({'ID': 'fedora'}, '7.2.11-test')
        self.assertEqual(cmd[:2], ['dnf', 'install'])
        self.assertIn('kernel-devel-7.2.11-test', cmd)
        self.assertIn('python3-gobject', cmd)

    def test_unsupported_distro_does_not_guess_package_manager(self):
        with self.assertRaises(ValueError):
            setup.dependency_command({'ID': 'unknown'}, 'test')


class InitramfsTests(unittest.TestCase):
    def choose(self, names, distro='cachyos'):
        return setup.initramfs_command({'ID': distro}, lambda name: '/usr/bin/' + name if name in names else None)

    def test_limine_uses_its_boot_entry_wrapper(self):
        self.assertEqual(self.choose(['limine-mkinitcpio', 'mkinitcpio']), ['/usr/bin/limine-mkinitcpio'])
        self.assertEqual(self.choose(['limine-dracut', 'dracut']), ['/usr/bin/limine-dracut'])

    def test_mkinitcpio_and_dracut(self):
        self.assertEqual(self.choose(['mkinitcpio']), ['/usr/bin/mkinitcpio', '--allpresets'])
        self.assertEqual(self.choose(['dracut']), ['/usr/bin/dracut', '--regenerate-all', '--force'])
        self.assertEqual(self.choose(['dracut'], 'fedora'), ['/usr/bin/dracut', '--regenerate-all', '--force'])

    def test_booster(self):
        path = '/usr/lib/booster/regenerate_images'
        self.assertEqual(setup.initramfs_command({'ID': 'arch'}, lambda p: p if p == path else None), [path])

    def test_missing_generator_fails(self):
        with self.assertRaises(ValueError):
            self.choose([])


class KernelBuildTests(unittest.TestCase):
    def test_missing_and_mismatched_headers_report_reboot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIn('missing', setup.header_problem(root, 'old'))
            (root / 'include/config').mkdir(parents=True)
            (root / 'include/config/kernel.release').write_text('new\n')
            self.assertIn('Reboot', setup.header_problem(root, 'old'))
            (root / 'Makefile').touch()
            (root / 'include/config/auto.conf').touch()
            self.assertIsNone(setup.header_problem(root, 'new'))

    def test_make_selects_llvm_for_clang_headers_only(self):
        makefile = Path(__file__).parent / 'kernel/Makefile'
        for config, expected in [('CONFIG_CC_IS_CLANG=y\n', '1'), ('CONFIG_CC_IS_GCC=y\n', '')]:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'include/config').mkdir(parents=True)
                (root / 'include/config/auto.conf').write_text(config)
                (root / 'Makefile').write_text('modules:\n\t@printf "LLVM=%s\\n" "$(LLVM)"\n')
                result = subprocess.run(['make', '--no-print-directory', '-f', str(makefile), 'KDIR=' + directory],
                                        check=True, text=True, capture_output=True)
                self.assertIn('LLVM=' + expected + '\n', result.stdout)


if __name__ == '__main__':
    unittest.main()
