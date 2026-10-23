// SPDX-License-Identifier: GPL-3.0-or-later
/* PHN16-71 firmware controls. WMI encodings documented by Linux acer-wmi,
 * Linuwu-Sense and facer. No raw EC access, arbitrary WMI access, boot-time
 * settings, overclock writes or firmware flashing. See ../README.md.
 */
#include <linux/acpi.h>
#include <linux/dmi.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>
#include <linux/suspend.h>
#include <linux/workqueue.h>

#define GAMING_GUID "7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56"
#define BATTERY_GUID "79772EC5-04B1-4BFD-843C-61E7F77B6CC9"
#define LEASE (45 * HZ)
static DEFINE_MUTEX(control_lock);
static struct platform_device *pdev;
static bool override_active;
static bool recovery_pending;
static bool suspending;
static unsigned long last_keepalive;
static struct delayed_work watchdog;

/* All callers hold control_lock. Firmware success and transport success are
 * separate: reject malformed results and the nonzero firmware status byte.
 */
static int call(u32 method, u64 arg, bool set, u64 *value)
{
    u32 small = arg;
    struct acpi_buffer in = { set ? sizeof(arg) : sizeof(small),
                             set ? (void *)&arg : (void *)&small };
    struct acpi_buffer out = { ACPI_ALLOCATE_BUFFER, NULL };
    union acpi_object *obj;
    u64 result = 0;
    int err = 0;
    acpi_status status = wmi_evaluate_method(GAMING_GUID, 0, method, &in, &out);
    obj = out.pointer;
    if (ACPI_FAILURE(status)) {
        err = -EIO;
    } else if (!obj) {
        err = -ENOMSG;
    } else if (obj->type == ACPI_TYPE_INTEGER) {
        result = obj->integer.value;
    } else if (obj->type == ACPI_TYPE_BUFFER && obj->buffer.pointer &&
               (obj->buffer.length == 4 || obj->buffer.length == 8)) {
        memcpy(&result, obj->buffer.pointer, obj->buffer.length);
    } else {
        err = -ENOMSG;
    }
    kfree(out.pointer);
    if (!err && (result & 0xff))
        err = -EREMOTEIO;
    if (!err && value)
        *value = result;
    return err;
}

static int get_byte(u32 method, u32 arg)
{
    u64 result;
    int err = call(method, arg, false, &result);
    return err ? err : (result >> 8) & 0xff;
}

static int get_mode(bool gpu)
{
    u64 result;
    int err = call(15, gpu ? 8 : 1, false, &result);
    int mode;
    if (err)
        return err;
    mode = (result >> (gpu ? 14 : 8)) & 3;
    return mode >= 1 && mode <= 3 ? mode : -ENODATA;
}

static int get_rpm(bool gpu)
{
    u64 result;
    int err = call(5, gpu ? 0x0601 : 0x0201, false, &result);
    return err ? err : (result >> 8) & 0xffff;
}

static int set_mode(unsigned int mode)
{
    return call(14, 9ULL | ((u64)mode << 16) | ((u64)mode << 22), true, NULL);
}

static int automatic(void)
{
    int err = set_mode(1);
    if (!err && (get_mode(false) != 1 || get_mode(true) != 1))
        err = -EIO;
    if (!err) {
        override_active = false;
        recovery_pending = false;
    } else {
        override_active = true;
        recovery_pending = true;
    }
    return err;
}

static void watchdog_fn(struct work_struct *work)
{
    mutex_lock(&control_lock);
    if (!suspending && override_active &&
        (recovery_pending || time_after(jiffies, last_keepalive + LEASE))) {
        if (automatic())
            pr_err_ratelimited("neo_control: firmware auto-fan restore failed; retrying\n");
    }
    mutex_unlock(&control_lock);
    schedule_delayed_work(&watchdog, 5 * HZ);
}

/* Unknown telemetry is JSON null, never a fabricated zero. */
static void number(char *buf, size_t n, int value)
{
    if (value < 0)
        strscpy(buf, "null", n);
    else
        scnprintf(buf, n, "%d", value);
}

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    char v[8][16];
    int cpu, gpu;
    mutex_lock(&control_lock);
    if (suspending) {
        mutex_unlock(&control_lock);
        return -EBUSY;
    }
    number(v[0], 16, get_byte(23, 11));
    number(v[1], 16, get_byte(23, 10));
    number(v[2], 16, get_rpm(false));
    number(v[3], 16, get_rpm(true));
    number(v[4], 16, get_mode(false));
    number(v[5], 16, get_mode(true));
    cpu = get_byte(17, 1);
    gpu = get_byte(17, 4);
    number(v[6], 16, cpu <= 100 ? cpu : -ENODATA);
    number(v[7], 16, gpu <= 100 ? gpu : -ENODATA);
    mutex_unlock(&control_lock);
    return sysfs_emit(buf, "{\"profile\":%s,\"profile_mask\":%s,\"cpu_rpm\":%s,\"gpu_rpm\":%s,\"cpu_mode\":%s,\"gpu_mode\":%s,\"cpu_percent\":%s,\"gpu_percent\":%s}\n",
                      v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]);
}
static DEVICE_ATTR_RO(status);

static ssize_t fan_control_store(struct device *dev, struct device_attribute *attr,
                                 const char *buf, size_t count)
{
    unsigned int cpu, gpu;
    char tail;
    int err = 0;
    bool manual = sscanf(buf, "manual %u %u %c", &cpu, &gpu, &tail) == 2;
    if (!sysfs_streq(buf, "auto") && !sysfs_streq(buf, "max") && !manual)
        return -EINVAL;
    if (manual && (cpu < 30 || cpu > 100 || gpu < 30 || gpu > 100))
        return -EINVAL;
    mutex_lock(&control_lock);
    if (suspending) {
        err = -EBUSY;
        goto out;
    }
    if (sysfs_streq(buf, "auto")) {
        err = automatic();
        goto out;
    }
    if (recovery_pending) {
        err = -EBUSY;
        goto out;
    }
    /* Arm recovery before any multi-command write, including partial failure. */
    override_active = true;
    last_keepalive = jiffies;
    err = set_mode(manual ? 3 : 2);
    if (!err && manual)
        err = call(16, 1 | ((u64)cpu << 8), true, NULL);
    if (!err && manual)
        err = call(16, 4 | ((u64)gpu << 8), true, NULL);
    if (!err && (get_mode(false) != (manual ? 3 : 2) ||
                 get_mode(true) != (manual ? 3 : 2)))
        err = -EIO;
    if (!err && manual && (get_byte(17, 1) != cpu || get_byte(17, 4) != gpu))
        err = -EIO;
    if (err && automatic())
        pr_err("neo_control: failed fan change and auto restore; watchdog will retry\n");
out:
    mutex_unlock(&control_lock);
    return err ? err : count;
}
static DEVICE_ATTR_WO(fan_control);

static ssize_t keepalive_store(struct device *dev, struct device_attribute *attr,
                               const char *buf, size_t count)
{
    if (!sysfs_streq(buf, "1"))
        return -EINVAL;
    mutex_lock(&control_lock);
    if (suspending || recovery_pending) {
        mutex_unlock(&control_lock);
        return -EBUSY;
    }
    last_keepalive = jiffies;
    mutex_unlock(&control_lock);
    return count;
}
static DEVICE_ATTR_WO(keepalive);

static ssize_t profile_store(struct device *dev, struct device_attribute *attr,
                             const char *buf, size_t count)
{
    unsigned int value;
    int mask, old, err;
    if (kstrtouint(buf, 10, &value) ||
        (value != 0 && value != 1 && value != 4 && value != 5 && value != 6))
        return -EINVAL;
    /* Acer's AC-only profiles; unknown power state must not permit them. */
    if ((value == 0 || value == 4 || value == 5) && power_supply_is_system_supplied() <= 0)
        return -EPERM;
    mutex_lock(&control_lock);
    if (suspending) {
        err = -EBUSY;
        goto out;
    }
    mask = get_byte(23, 10);
    old = get_byte(23, 11);
    if (mask < 0 || old < 0) {
        err = -EIO;
        goto out;
    }
    if (!(mask & BIT(value))) {
        err = -EOPNOTSUPP;
        goto out;
    }
    err = automatic();
    if (err)
        goto out;
    err = call(22, 11 | ((u64)value << 8), true, NULL);
    if (!err && get_byte(23, 11) != value)
        err = -EIO;
    if (err && (call(22, 11 | ((u64)old << 8), true, NULL) || get_byte(23, 11) != old))
        pr_err("neo_control: profile rollback failed\n");
out:
    mutex_unlock(&control_lock);
    return err ? err : count;
}
static DEVICE_ATTR_WO(profile);

/* Typed WMI buffers used by the battery and four-zone lighting interfaces. */
static int buffer_call(const char *guid, u32 method, void *data, size_t size,
                       u8 *reply, size_t reply_size)
{
    struct acpi_buffer in = { size, data };
    struct acpi_buffer out = { ACPI_ALLOCATE_BUFFER, NULL };
    union acpi_object *obj;
    int err = 0;
    acpi_status status = wmi_evaluate_method(guid, 0, method, &in, &out);
    obj = out.pointer;
    if (ACPI_FAILURE(status))
        err = -EIO;
    else if (obj && obj->type == ACPI_TYPE_BUFFER && obj->buffer.pointer &&
             obj->buffer.length == reply_size)
        memcpy(reply, obj->buffer.pointer, reply_size);
    else if (obj && obj->type == ACPI_TYPE_INTEGER && reply_size == 4) {
        u32 value = obj->integer.value;
        memcpy(reply, &value, 4);
    } else
        err = -ENOMSG;
    kfree(out.pointer);
    return err;
}

static int get_battery_limit(void)
{
    u8 in[4] = {1, 1, 0, 0}, out[8];
    int err = buffer_call(BATTERY_GUID, 20, in, 4, out, 8);
    if (err)
        return err;
    if (!(out[0] & 1))
        return -EOPNOTSUPP;
    if (out[1] || out[2] || out[3] > 1)
        return -EREMOTEIO;
    return out[3];
}

static ssize_t battery_limit_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    int value;
    mutex_lock(&control_lock);
    value = suspending ? -EBUSY : get_battery_limit();
    mutex_unlock(&control_lock);
    return value < 0 ? value : sysfs_emit(buf, "%d\n", value);
}

static ssize_t battery_limit_store(struct device *dev, struct device_attribute *attr,
                                   const char *buf, size_t count)
{
    unsigned int value;
    u8 in[8] = {1, 1, 0, 0, 0, 0, 0, 0}, out[4];
    int err;
    if (kstrtouint(buf, 10, &value) || value > 1)
        return -EINVAL;
    in[2] = value;
    mutex_lock(&control_lock);
    err = suspending ? -EBUSY : get_battery_limit();
    if (err >= 0) {
        err = buffer_call(BATTERY_GUID, 21, in, 8, out, 4);
        if (!err && (out[0] || get_battery_limit() != value))
            err = -EREMOTEIO;
    }
    mutex_unlock(&control_lock);
    return err ? err : count;
}
static DEVICE_ATTR_RW(battery_limit);

static int get_lighting(u8 *out)
{
    u64 in = 1;
    int err = buffer_call(GAMING_GUID, 21, &in, 8, out, 16);
    if (!err && out[0])
        err = -EREMOTEIO;
    return err;
}

static int set_lighting(u8 *in)
{
    u8 reply[4];
    int err = buffer_call(GAMING_GUID, 20, in, 16, reply, 4);
    if (!err && (reply[0] || reply[1] || reply[2] || reply[3]))
        err = -EREMOTEIO;
    return err;
}

static int get_zones(u32 *colors);

static int set_zone(int zone, u32 color)
{
    u64 payload = BIT(zone) | (((color >> 16) & 255ULL) << 8) |
                  (((color >> 8) & 255ULL) << 16) | ((color & 255ULL) << 24);
    return call(6, payload, true, NULL);
}

static ssize_t lighting_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    u8 out[16];
    u32 colors[4];
    int err;
    mutex_lock(&control_lock);
    err = suspending ? -EBUSY : get_lighting(out);
    /* Static mode uses the separate zone registers; the RGB bytes in method
     * 21 otherwise retain the last animated color on this PHN16-71. */
    if (!err && out[1] == 0) {
        err = get_zones(colors);
        if (!err) {
            out[6] = colors[0] >> 16;
            out[7] = colors[0] >> 8;
            out[8] = colors[0];
        }
    }
    mutex_unlock(&control_lock);
    if (err)
        return err;
    return sysfs_emit(buf, "{\"mode\":%u,\"speed\":%u,\"brightness\":%u,\"direction\":%u,\"red\":%u,\"green\":%u,\"blue\":%u}\n",
                      out[1], out[2], out[3], out[5], out[6], out[7], out[8]);
}

static ssize_t lighting_store(struct device *dev, struct device_attribute *attr,
                              const char *buf, size_t count)
{
    unsigned int mode, speed, bright, direction, r, g, b;
    char tail;
    u8 in[16] = {0}, out[16];
    u32 colors[4], color;
    int err, i;
    if (sscanf(buf, "%u %u %u %u %u %u %u %c", &mode, &speed, &bright,
               &direction, &r, &g, &b, &tail) != 7 ||
        mode > 7 || speed > 9 || bright > 100 || direction > 2 ||
        r > 255 || g > 255 || b > 255)
        return -EINVAL;
    if (mode == 0) speed = 0;
    if (mode != 3 && mode != 4) direction = 0;
    if (mode == 2 || mode == 3) r = g = b = 0;
    in[0] = mode; in[1] = speed; in[2] = bright; in[4] = direction;
    in[5] = r; in[6] = g; in[7] = b; in[8] = 3; in[9] = 1;
    color = (r << 16) | (g << 8) | b;
    mutex_lock(&control_lock);
    err = suspending ? -EBUSY : set_lighting(in);
    if (!err && mode == 0) {
        for (i = 0; !err && i < 4; i++) err = set_zone(i, color);
        if (!err) err = get_zones(colors);
        for (i = 0; !err && i < 4; i++)
            if (colors[i] != color) err = -EIO;
    }
    if (!err) err = get_lighting(out);
    if (!err && (out[1] != mode || out[3] != bright ||
                 (mode > 0 && out[2] != speed) ||
                 ((mode == 3 || mode == 4) && out[5] != direction) ||
                 ((mode != 0 && mode != 2 && mode != 3) &&
                  (out[6] != r || out[7] != g || out[8] != b))))
        err = -EIO;
    mutex_unlock(&control_lock);
    return err ? err : count;
}
static DEVICE_ATTR_RW(lighting);

static int get_zones(u32 *colors)
{
    int i, err;
    u64 out;
    for (i = 0; i < 4; i++) {
        /* Method 7 uses an eight-byte zone selector. */
        err = call(7, BIT(i), true, &out);
        if (err) return err;
        colors[i] = (((out >> 8) & 255) << 16) |
                    (((out >> 16) & 255) << 8) | ((out >> 24) & 255);
    }
    return 0;
}

static ssize_t zones_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    u32 colors[4];
    u8 out[16];
    int err;
    mutex_lock(&control_lock);
    err = suspending ? -EBUSY : get_zones(colors);
    if (!err) err = get_lighting(out);
    mutex_unlock(&control_lock);
    if (err) return err;
    return sysfs_emit(buf, "{\"colors\":[\"%06x\",\"%06x\",\"%06x\",\"%06x\"],\"brightness\":%u}\n",
                      colors[0], colors[1], colors[2], colors[3], out[3]);
}

static ssize_t zones_store(struct device *dev, struct device_attribute *attr,
                           const char *buf, size_t count)
{
    u32 colors[4], check[4];
    unsigned int bright;
    char tail;
    u8 in[16] = {0}, out[16];
    int i, err;
    if (sscanf(buf, "%x %x %x %x %u %c", &colors[0], &colors[1], &colors[2],
               &colors[3], &bright, &tail) != 5 || bright > 100)
        return -EINVAL;
    for (i = 0; i < 4; i++)
        if (colors[i] > 0xffffff) return -EINVAL;
    in[2] = bright; in[8] = 3; in[9] = 1;
    mutex_lock(&control_lock);
    err = suspending ? -EBUSY : set_lighting(in);
    for (i = 0; !err && i < 4; i++) {
        err = set_zone(i, colors[i]);
    }
    if (!err) err = get_zones(check);
    if (!err) err = get_lighting(out);
    if (!err && (memcmp(colors, check, sizeof(colors)) || out[3] != bright || out[1] != 0))
        err = -EIO;
    mutex_unlock(&control_lock);
    return err ? err : count;
}
static DEVICE_ATTR_RW(zones);

static struct attribute *neo_attrs[] = {
    &dev_attr_status.attr, &dev_attr_fan_control.attr,
    &dev_attr_profile.attr, &dev_attr_keepalive.attr,
    &dev_attr_battery_limit.attr, &dev_attr_lighting.attr, &dev_attr_zones.attr, NULL
};
ATTRIBUTE_GROUPS(neo);

static int power_event(struct notifier_block *nb, unsigned long event, void *unused)
{
    if (event == PM_SUSPEND_PREPARE || event == PM_HIBERNATION_PREPARE ||
        event == PM_RESTORE_PREPARE) {
        int err = 0;
        mutex_lock(&control_lock);
        suspending = true;
        if (override_active)
            err = automatic();
        if (err)
            suspending = false;
        mutex_unlock(&control_lock);
        if (err)
            return NOTIFY_BAD;
    } else if (event == PM_POST_SUSPEND || event == PM_POST_HIBERNATION ||
               event == PM_POST_RESTORE) {
        mutex_lock(&control_lock);
        suspending = false;
        mutex_unlock(&control_lock);
    }
    return NOTIFY_OK;
}
static struct notifier_block pm_notifier = { .notifier_call = power_event };

static int __init neo_init(void)
{
    struct device *existing;
    int err, mask;
    if (!dmi_match(DMI_SYS_VENDOR, "Acer") ||
        !dmi_match(DMI_PRODUCT_NAME, "Predator PHN16-71") ||
        !wmi_has_guid(GAMING_GUID))
        return -ENODEV;
    existing = bus_find_device_by_name(&platform_bus_type, NULL, "acer-wmi");
    if (existing) {
        put_device(existing);
        pr_err("neo_control: unload competing Acer control driver first\n");
        return -EBUSY;
    }
    mutex_lock(&control_lock);
    mask = get_byte(23, 10);
    mutex_unlock(&control_lock);
    if (mask < 0)
        return mask;
    pdev = platform_device_register_simple("neo-control", -1, NULL, 0);
    if (IS_ERR(pdev))
        return PTR_ERR(pdev);
    err = sysfs_create_groups(&pdev->dev.kobj, neo_groups);
    if (err)
        goto fail;
    err = register_pm_notifier(&pm_notifier);
    if (err) {
        sysfs_remove_groups(&pdev->dev.kobj, neo_groups);
        goto fail;
    }
    INIT_DELAYED_WORK(&watchdog, watchdog_fn);
    schedule_delayed_work(&watchdog, 5 * HZ);
    pr_info("neo_control: PHN16-71 ready, supported profiles 0x%x; no settings changed\n", mask);
    return 0;
fail:
    platform_device_unregister(pdev);
    return err;
}

static void __exit neo_exit(void)
{
    unregister_pm_notifier(&pm_notifier);
    sysfs_remove_groups(&pdev->dev.kobj, neo_groups);
    cancel_delayed_work_sync(&watchdog);
    mutex_lock(&control_lock);
    if (override_active && automatic())
        pr_err("neo_control: could not restore automatic fans on unload\n");
    mutex_unlock(&control_lock);
    platform_device_unregister(pdev);
}
module_init(neo_init);
module_exit(neo_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("Neo Control contributors");
MODULE_DESCRIPTION("Model-specific Acer PHN16-71 fan and thermal controls");
MODULE_VERSION("0.1.0");
