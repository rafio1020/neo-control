# CachyOS setup for Neo Control

For **Acer Predator Helios Neo 16 PHN16-71** only. The app works with KDE Plasma, GNOME and Xfce. CachyOS kernel compilation and DKMS installation have passed in a test container; a physical CachyOS boot and fan test is still pending.

## 1. Update CachyOS and install Git

```sh
sudo pacman -Syu --needed git
```

If this upgrades your kernel, **reboot before continuing**.

## 2. Download Neo Control and install dependencies

```sh
git clone https://github.com/rafio1020/neo-control.git
cd neo-control
./install.sh --install-deps
```

This installs the GTK4/Python runtime, DKMS, build tools and headers for your running kernel variant. It also performs a full system update. If the kernel changes, reboot again, then return to the `neo-control` directory.

## 3. Check and install

Run these as your normal desktop user:

```sh
./install.sh --check
./install.sh
```

The installer asks for administrator authentication when needed. It adds your user to the control group and creates the application launcher.

- If it reports that another Acer driver is active, **reboot** to switch to Neo Control.
- If it requests a new login for group permissions, log out and back in, or reboot.
- If it reports an error, resolve that error before proceeding. Kernel/header mismatches usually require installing matching headers or rebooting into the updated kernel.

## 4. Open and verify

Open **Neo Control** from your application menu. For diagnostics:

```sh
systemctl status neo-control.service --no-pager
python3 /opt/neo-control/neo_control.py --diagnose
```

Check that fan RPM and temperature readings appear. Select a fan mode or use **Custom → Apply manual speeds**. Closing the window keeps cooling running in the background; **Menu → Quit and restore Auto** stops the controller.

To bind the PredatorSense button, add a keyboard shortcut in your desktop settings using this command, then press the button to assign it:

```sh
/usr/bin/python3 /opt/neo-control/neo_control.py
```

On this laptop the key is `XF86Launch1`. In KDE Plasma, use **System Settings → Keyboard → Shortcuts**; in GNOME, use **Settings → Keyboard → Custom Shortcuts**.

## Update or remove

First choose **Menu → Quit and restore Auto**. To update, run these in the cloned directory:

```sh
git pull --ff-only
./install.sh
```

To remove the driver and launcher:

```sh
./uninstall.sh
```

Follow any reboot instructions. See the [full README](README.md#kernel-and-boot-support) for Secure Boot signing, additional kernels, competing drivers and boot-image troubleshooting, and the [test results](evidence/distro-compatibility.json) for the exact validation scope.
