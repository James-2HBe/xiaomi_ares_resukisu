#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
SukiSU-Ultra (main/dev, v4.3.x) -> Linux 4.14 non-GKI compatibility patcher.

Why:
  build.log fails with 5 errors in drivers/kernelsu:
    hook/syscall_hook.h:11   unknown type name 'syscall_fn_t'
    hook/syscall_hook.h:42   unknown type name 'syscall_fn_t' (x2)
    core/init.c:282          MODULE_IMPORT_NS ... implicit-int / parameter list
  Both are 5.x+/6.x-only APIs missing on a 4.14 kernel.

What this script does (all idempotent, all optional per file):
  1. core/init.c (or ksu.c on older layouts):
     guard MODULE_IMPORT_NS with LINUX_VERSION_CODE >= 5.4.
  2. hook/syscall_hook.h:
     provide a syscall_fn_t typedef for arm64 kernels < 4.17.
  3. hook/arm64/syscall_hook.c:
     replace the pt_regs-based syscall dispatcher with no-op stubs.
     The real implementation needs __arm64_sys_ni_syscall / syscallno
     redirection, which do not exist on 4.14; patching sys_call_table
     there would corrupt it at runtime.

Usage in a GitHub Actions step (after setup.sh, before make):
  cd kernel
  python3 $GITHUB_WORKSPACE/patch_ksu_414.py drivers/kernelsu

  Or let it auto-detect:
  python3 $GITHUB_WORKSPACE/patch_ksu_414.py

Exit code is 0 even if a target file is absent (old KSU layouts), so it is
safe to run against main, dev, old or pinned tags.
"""

import os
import sys

MARK = "KSU_414_COMPAT"

OLD_IMPORT_NS = """#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 13, 0)
MODULE_IMPORT_NS("VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver");
#else
MODULE_IMPORT_NS(VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver);
#endif"""

NEW_IMPORT_NS = """/* KSU_414_COMPAT: MODULE_IMPORT_NS was added in Linux 5.4.
 * 4.14 has no module namespace support, so the macro must not be emitted. */
#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 4, 0)
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 13, 0)
MODULE_IMPORT_NS("VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver");
#else
MODULE_IMPORT_NS(VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver);
#endif
#endif"""

OLD_TYPEDEF = """#if defined(__x86_64__)
typedef sys_call_ptr_t syscall_fn_t;
#elif defined(__riscv)
typedef long (*syscall_fn_t)(const struct pt_regs *);
#endif"""

NEW_TYPEDEF = """#if defined(__x86_64__)
typedef sys_call_ptr_t syscall_fn_t;
#elif defined(__riscv)
typedef long (*syscall_fn_t)(const struct pt_regs *);
#elif defined(__aarch64__)
/* KSU_414_COMPAT: syscall_fn_t arrived with the pt_regs-based syscall ABI
 * in Linux 4.17. On 4.14 arm64 sys_call_table holds old-style handlers
 * taking up to six unsigned long arguments (x0..x5). */
#include <linux/version.h>
#if LINUX_VERSION_CODE < KERNEL_VERSION(4, 17, 0)
typedef long (*syscall_fn_t)(unsigned long, unsigned long, unsigned long,
                             unsigned long, unsigned long, unsigned long);
#endif
#endif"""

STUB_C = """/* KSU_414_COMPAT: Linux 4.14 arm64 compatibility stubs.
 *
 * SukiSU-Ultra main's syscall dispatcher depends on the 4.17+ arm64
 * pt_regs-based syscall ABI (the __arm64_sys_* wrappers, the ni_syscall
 * wrapper symbol, sys_enter -> syscallno redirection). None of that
 * exists on 4.14, where sys_call_table stores old-style handlers.
 * Installing the dispatcher here
 * would patch a wrong-typed function pointer into sys_call_table and crash
 * the kernel on the first redirected syscall.
 *
 * Keep every symbol declared in hook/syscall_hook.h, but make the whole
 * subsystem a no-op. ksu_dispatcher_nr stays -1, so the sys_enter handler
 * in syscall_hook_manager.c returns immediately and never redirects.
 *
 * Trade-off: dispatcher-based hooks (setresuid/execve/newfstatat/faccessat,
 * sucompat) are inactive. Everything else in KSU still works.
 */
#include "../syscall_hook.h"

#include <linux/init.h>
#include <linux/types.h>
#include <linux/errno.h>
#include <linux/kernel.h>

syscall_fn_t *ksu_syscall_table = NULL;
int ksu_dispatcher_nr = -1;

int ksu_register_syscall_hook(int nr, ksu_syscall_hook_fn fn)
{
\t(void)nr;
\t(void)fn;
\treturn -ENOSYS;
}

void ksu_unregister_syscall_hook(int nr)
{
\t(void)nr;
}

bool ksu_has_syscall_hook(int nr)
{
\t(void)nr;
\treturn false;
}

void ksu_syscall_table_hook(int nr, syscall_fn_t fn, syscall_fn_t *old)
{
\t(void)nr;
\t(void)fn;
\t(void)old;
}

void ksu_syscall_table_unhook(int nr)
{
\t(void)nr;
}

void __init ksu_syscall_hook_init(void)
{
\tpr_info("KSU: syscall dispatcher disabled on 4.14 (KSU_414_COMPAT)\\n");
}

void __exit ksu_syscall_hook_exit(void)
{
}
"""


def read(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def replace_once(root, relpaths, old, new, label):
    for rel in relpaths:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        text = read(path)
        if MARK in text:
            print("[skip] %s already patched (%s)" % (rel, label))
            return True
        if old not in text:
            print("[warn] %s: pattern not found (%s)" % (rel, label))
            return False
        write(path, text.replace(old, new, 1))
        print("[ ok ] %s patched (%s)" % (rel, label))
        return True
    print("[skip] no candidate file for %s" % label)
    return False


def install_stub(root, relpath):
    path = os.path.join(root, relpath)
    if not os.path.isfile(path):
        print("[skip] %s not present (old KSU layout)" % relpath)
        return False
    text = read(path)
    if MARK in text:
        print("[skip] %s already stubbed" % relpath)
        return True
    write(path, STUB_C)
    print("[ ok ] %s replaced with 4.14-safe stubs" % relpath)
    return True


def find_root(arg):
    if arg:
        return arg
    for candidate in (
        "drivers/kernelsu",
        "kernel/drivers/kernelsu",
        "common/drivers/kernelsu",
        "KernelSU/kernel",
        "kernel/KernelSU/kernel",
    ):
        if os.path.isdir(candidate):
            return candidate
    return "drivers/kernelsu"


def main():
    root = find_root(sys.argv[1] if len(sys.argv) > 1 else None)
    print("KSU driver dir: %s (exists=%s)" % (root, os.path.isdir(root)))
    if not os.path.isdir(root):
        print("[error] KSU driver directory not found; pass it as argv[1]")
        return 1

    replace_once(
        root,
        ("core/init.c", "ksu.c"),
        OLD_IMPORT_NS,
        NEW_IMPORT_NS,
        "MODULE_IMPORT_NS version guard",
    )
    replace_once(
        root,
        ("hook/syscall_hook.h",),
        OLD_TYPEDEF,
        NEW_TYPEDEF,
        "arm64 syscall_fn_t typedef",
    )
    install_stub(root, "hook/arm64/syscall_hook.c")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
