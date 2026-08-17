import os
import subprocess
import logging
import re
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field

from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   ANYKERNEL_CONFIG, KERNEL_PATCHES_CONFIG, BBG_CONFIG, TOOLCHAIN_CONFIG,
                   LEGACY_FIXES, OP8E_PATCH_URL, KPM_PATCH_URL)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    success: bool
    config: BuildConfig
    message: str = ""
    artifacts: list = field(default_factory=list)
    build_time: Optional[float] = None


class ShellCommand:
    def __init__(self, cwd: Optional[str] = None, env: Optional[dict] = None):
        self.cwd = cwd
        self.env = env or os.environ.copy()

    def run(self, cmd: str, check: bool = True, capture_output: bool = False,
            shell: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        logger.info(f"执行命令: {cmd}")
        try:
            return subprocess.run(cmd, shell=shell, cwd=self.cwd, env=self.env,
                                capture_output=capture_output, text=True, timeout=timeout, check=check)
        except subprocess.CalledProcessError as e:
            logger.error(f"命令执行失败: {e.stderr or str(e)}")
            raise
        except subprocess.TimeoutExpired:
            logger.error(f"命令执行超时: {cmd}")
            raise

    def run_with_callback(self, cmd: str, callback: Optional[Callable] = None) -> str:
        logger.info(f"执行命令: {cmd}")
        process = subprocess.Popen(cmd, shell=True, cwd=self.cwd, env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output_lines = []
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if callback:
                callback(line)
        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"命令执行失败")
        return "\n".join(output_lines)


class KernelBuilder:
    KERNEL_CONFIG_TEMPLATE = """
# === KernelSU Config ===
CONFIG_KSU=y
CONFIG_KPM=y
CONFIG_KSU_SUSFS_SUS_SU=n

# === TMPFS Config ===
CONFIG_TMPFS_XATTR=y
CONFIG_TMPFS_POSIX_ACL=y

# === Network Config ===
CONFIG_IP_NF_TARGET_TTL=y
CONFIG_IP6_NF_TARGET_HL=y
CONFIG_IP6_NF_MATCH_HL=y

# === BBR Config ===
CONFIG_TCP_CONG_ADVANCED=y
CONFIG_TCP_CONG_BBR=y
CONFIG_NET_SCH_FQ=y
CONFIG_TCP_CONG_BIC=n
CONFIG_TCP_CONG_WESTWOOD=n
CONFIG_TCP_CONG_HTCP=n

# === SUSFS Config ===
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_TRY_UMOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
"""

    ZRAM_CONFIG_5_10 = "CONFIG_ZSMALLOC=y\nCONFIG_ZRAM=y\nCONFIG_MODULE_SIG=n\nCONFIG_CRYPTO_LZO=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
    ZRAM_CONFIG_COMMON = "CONFIG_CRYPTO_LZ4HC=y\nCONFIG_CRYPTO_LZ4K=y\nCONFIG_CRYPTO_LZ4KD=y\nCONFIG_CRYPTO_842=y\nCONFIG_CRYPTO_LZ4K_OPLUS=y\nCONFIG_ZRAM_WRITEBACK=y\n"

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
        self.workspace = Path(workspace)
        self.shell = ShellCommand(cwd=workspace)
        self.env = os.environ.copy()
        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.kernel_patches_dir = self.workspace / "kernel_patches"
        self.toolchain_dir = self.workspace / "toolchain"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        self._setup_env()

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.shell.env = self.env

    def _run_cmd(self, cmd: str, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, **kwargs)

    def _chdir(self, path: Path):
        os.chdir(path)
        self.shell.cwd = str(path)

    def _apply_susfs_commit(self):
        if not self.config.susfs_commit or not self.susfs_dir.exists():
            return
        self._chdir(self.susfs_dir)
        if self.config.susfs_commit.startswith("HEAD~"):
            self._run_cmd("git fetch origin", check=False)
            self._run_cmd(f"git reset --hard {self.config.susfs_commit}", check=False)
        else:
            self._run_cmd("git fetch origin", check=False)
            self._run_cmd(f"git checkout {self.config.susfs_commit}", check=False)
        self._chdir(self.workspace)

    def clone_repositories(self):
        logger.info("=== 开始克隆仓库 ===")
        for name, repo_dir, url, branch in [
            ("SUSFS", self.susfs_dir, SUSFS_REPO_CONFIG['repo_url'], self.config.kernel_branch),
            ("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG['repo_url'], None),
            ("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG['repo_url'], ANYKERNEL_CONFIG['branch']),
            ("Kernel Patches", self.kernel_patches_dir, KERNEL_PATCHES_CONFIG['repo_url'], None),
        ]:
            if not repo_dir.exists():
                cmd = f"git clone {url}"
                if branch:
                    cmd += f" -b {branch}"
                logger.info(f"克隆 {name}...")
                self._run_cmd(cmd, check=False)
            else:
                logger.info(f"{name} 已存在，跳过")
        self._apply_susfs_commit()
        logger.info("=== 仓库克隆完成 ===")

    def clone_toolchain(self):
        logger.info("=== 克隆工具链 ===")
        if not self.toolchain_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/kernel/prebuilts/build-tools "
                         f"-b {TOOLCHAIN_CONFIG['build_tools_branch']} --depth 1 {self.toolchain_dir}", check=False)
        if not self.mkbootimg_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/platform/system/tools/mkbootimg "
                         f"-b {TOOLCHAIN_CONFIG['mkbootimg_branch']} --depth 1 {self.mkbootimg_dir}", check=False)
        self.env["AVBTOOL"] = str(self.toolchain_dir / "linux-x86/bin/avbtool")
        self.env["MKBOOTIMG"] = str(self.mkbootimg_dir / "mkbootimg.py")
        self.env["UNPACK_BOOTIMG"] = str(self.mkbootimg_dir / "unpack_bootimg.py")
        if "BOOT_SIGN_KEY_PATH" in os.environ:
            self.env["BOOT_SIGN_KEY_PATH"] = os.environ["BOOT_SIGN_KEY_PATH"]
        self.shell.env = self.env
        logger.info("=== 工具链准备完成 ===")

    def setup_repo_tool(self):
        logger.info("=== 安装 repo 工具 ===")
        import subprocess
        import urllib.request
        import ssl
        import tempfile

        repo_path = "/tmp/repo"
        # 如果 repo 脚本已存在，直接复用（预装到仓库时可跳过下载）
        if os.path.isfile(repo_path) and os.access(repo_path, os.X_OK):
            logger.info(f"repo 工具已存在，复用: {repo_path}")
            with open(repo_path, "r") as f:
                first_line = f.readline().strip()
            logger.info(f"repo 文件首行: {first_line}")
            self.env["REPO"] = repo_path
            self.shell.env = self.env
            logger.info(f"repo 工具准备完成: {repo_path}")
            return

        downloaded = False

        # 尝试多个源下载 Google repo 脚本
        urls = [
            "https://gerrit.googlesource.com/git-repo/+archive/main/repo",
            "https://storage.googleapis.com/git-repo-downloads/repo",
        ]
        ssl_ctx = ssl.create_default_context()

        for url in urls:
            try:
                logger.info(f"正在从 {url} 下载 repo...")
                with urllib.request.urlopen(url, timeout=30, context=ssl_ctx) as resp:
                    content = resp.read()
                with open(repo_path, "wb") as f:
                    f.write(content)
                os.chmod(repo_path, 0o755)
                with open(repo_path, "r") as f:
                    first_line = f.readline().strip()
                logger.info(f"repo 文件首行: {first_line}")
                downloaded = True
                break
            except Exception as e:
                logger.warning(f"从 {url} 下载 repo 失败: {e}，尝试下一个源...")

        if not downloaded:
            # 最后 fallback: pip install
            try:
                logger.warning("所有直接下载方式均失败，尝试 pip install git-repo")
                subprocess.run(["pip", "install", "git-repo"], check=True)
                for p in ["/usr/local/bin/repo", "/usr/bin/repo"]:
                    if os.path.exists(p):
                        repo_path = p
                        break
                else:
                    out = subprocess.run(["python", "-m", "site", "--user-base"], capture_output=True, text=True)
                    user_base = out.stdout.strip()
                    repo_path = os.path.join(user_base, "bin", "repo")
                os.chmod(repo_path, 0o755)
            except Exception as e:
                raise RuntimeError(f"无法安装 repo 工具: {e}")

        self.env["REPO"] = repo_path
        self.shell.env = self.env
        logger.info(f"repo 工具安装成功: {repo_path}")

    def init_and_sync_kernel(self):
        logger.info("=== 初始化和同步内核源代码 ===")
        self._chdir(self.work_dir)
        formatted_branch = self.config.formatted_branch

        self._run_cmd(f"$REPO init --depth=1 --manifest-url=https://android.googlesource.com/kernel/manifest "
                     f"-b common-{formatted_branch}", check=False)

        # 检测是否需要 deprecated 分支前缀
        ls_remote = subprocess.run(
            f"git ls-remote --heads https://android.googlesource.com/kernel/common {formatted_branch} 2>&1",
            shell=True, capture_output=True, text=True
        )
        remote_output = (ls_remote.stdout + ls_remote.stderr).strip()

        # 检查是否存在 deprecated 前缀的分支
        deprecated_branch = f"deprecated/{formatted_branch}"
        ls_deprecated = subprocess.run(
            f"git ls-remote --heads https://android.googlesource.com/kernel/common {deprecated_branch} 2>&1",
            shell=True, capture_output=True, text=True
        )
        deprecated_output = (ls_deprecated.stdout + ls_deprecated.stderr).strip()

        if remote_output and not deprecated_output:
            # 正常分支存在，deprecated 不存在 → 无需替换
            logger.info(f"使用正常分支: {formatted_branch}")
        elif deprecated_output and not remote_output:
            # 只有 deprecated 分支 → 需要替换 manifest
            logger.info(f"使用 deprecated 分支: {deprecated_branch}")
            manifest_path = self.work_dir / ".repo/manifests/default.xml"
            if manifest_path.exists():
                with open(manifest_path, "r") as f:
                    content = f.read()
                content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
                with open(manifest_path, "w") as f:
                    f.write(content)
        elif deprecated_output and remote_output:
            # 两者都存在，优先使用 deprecated 分支（更稳定）
            logger.info(f"优先使用 deprecated 分支: {deprecated_branch}")
            manifest_path = self.work_dir / ".repo/manifests/default.xml"
            if manifest_path.exists():
                with open(manifest_path, "r") as f:
                    content = f.read()
                content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
                with open(manifest_path, "w") as f:
                    f.write(content)
        else:
            logger.warning(f"未找到分支 {formatted_branch} 或 deprecated/{formatted_branch}，尝试继续...")

        logger.info("同步内核源代码...")
        self._run_cmd("$REPO --trace sync -c -j$(nproc --all) --no-tags --fail-fast", check=False)

        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError("repo sync 失败，common 目录不存在")
        self._apply_legacy_fixes(deprecated_output)
        logger.info("=== 内核源代码同步完成 ===")

    def _apply_legacy_fixes(self, remote_branch: str = ""):
        av, kv = self.config.android_version, self.config.kernel_version
        sub = self.config.get_sub_level_int()
        is_deprecated = "deprecated" in remote_branch

        if is_deprecated and av == "android13" and kv == "5.15" and sub and sub < 123:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android13-5.15-below-123']['url']} -o fix.patch && patch -p1 < fix.patch", check=False)
            self._chdir(self.work_dir)

        if av == "android12" and kv == "5.10" and sub and sub < 136:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android12-5.10-below-136']['url']} | patch -p1", check=False)
            self._chdir(self.work_dir)

    def add_kernel_supatch(self):
        if not self.config.support_op8e:
            return
        logger.info("=== 添加 OnePlus 8E 支持补丁 ===")
        drivers_dir = self.work_dir / "common/drivers"
        if not drivers_dir.exists():
            return
        self._chdir(drivers_dir)
        self._run_cmd(f"curl -LSs {OP8E_PATCH_URL} -o hmbird_patch.c", check=False)
        if (drivers_dir / "hmbird_patch.c").exists():
            with open(drivers_dir / "Makefile", "a") as f:
                f.write("obj-y += hmbird_patch.o\n")

    def add_kernelsu(self):
        logger.info("=== 添加 KernelSU ===")
        self._chdir(self.work_dir)
        setup_url = (f"https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/{self.config.kernelsu_commit}/kernel/setup.sh"
                    if self.config.kernelsu_commit else KSU_REPO_CONFIG["setup_script"])
        self._run_cmd(f"curl -LSs {setup_url} | bash -s builtin", check=False)
        if self.config.kernelsu_commit:
            ksu_dir = self.work_dir / "KernelSU"
            if ksu_dir.exists():
                self._chdir(ksu_dir)
                self._run_cmd(f"git checkout {self.config.kernelsu_commit}", check=False)
                self._chdir(self.work_dir)

    def add_bbg(self):
        if not self.config.use_bbg:
            return
        logger.info("=== 添加 Baseband-guard ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return
        self._chdir(common_dir)
        self._run_cmd(f"wget -O- {BBG_CONFIG['setup_script']} | bash", check=False)
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if config_file.exists():
            with open(config_file, "a") as f:
                f.write("CONFIG_BBG=y\n")
        kconfig_file = common_dir / "security/Kconfig"
        if kconfig_file.exists():
            with open(kconfig_file, "r") as f:
                content = f.read()
            content = re.sub(r'(config LSM.*?)(default .*)(\n.*?help)',
                           lambda m: m.group(1) + ('lockdown,baseband_guard' if 'lockdown' in m.group(2) and 'baseband_guard' not in m.group(2) else m.group(2)) + m.group(3),
                           content, flags=re.DOTALL)
            with open(kconfig_file, "w") as f:
                f.write(content)

    def apply_susfs_patches(self):
        logger.info("=== 应用 SUSFS 补丁 ===")
        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        if susfs_patch.exists():
            self._run_cmd(f"cp {susfs_patch} {common_dir}/", check=False)
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs/"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux/"),
        ]:
            if src.exists():
                self._run_cmd(f"cp -r {src}/* {dst}", check=False)
        if susfs_patch.exists():
            patch_file = common_dir / self.config.get_susfs_patch_filename()
            if patch_file.exists():
                self._chdir(common_dir)
                self._run_cmd(f"patch -p1 --fuzz=3 < {patch_file}", check=False)
                self._chdir(self.work_dir)

    def apply_sukisu_patches(self):
        logger.info("=== 应用 SukiSU 补丁 ===")
        fb = f"{self.config.android_version}-{self.config.kernel_version}"
        # 69_hide_stuff.patch 是为 Android15/6.6 写的，直接 patch 到旧内核会因 fuzz
        # 错位导致 unused variable/label 编译错误，改为手动精确插入
        self._apply_sukisu_manual(fb)

    def _apply_sukisu_manual(self, fb):
        """手动将 SukiSU 的 lineage/jit-zygote-cache 隐藏逻辑精确插入内核源码"""
        self._chdir(self.work_dir / "common")
        import re

        # ===== task_mmu.c: show_map_vma =====
        task_mmu = Path("fs/proc/task_mmu.c")
        if task_mmu.exists():
            with open(task_mmu, "r") as f:
                content = f.read()

            if "show_vma_header_prefix_fake" not in content:
                # 1. 插入 show_vma_header_prefix_fake 函数
                fake_func = """static void show_vma_header_prefix_fake(struct seq_file *m,
					unsigned long start, unsigned long end,
					vm_flags_t flags, unsigned long long pgoff,
					dev_t dev, unsigned long ino)
{
	seq_setwidth(m, 25 + sizeof(void *) * 6 - 1);
	seq_printf(m, "%08lx-%08lx %c%c%c%c %08llx %02x:%02x %lu ",
			start, end,
			flags & VM_READ ? 'r' : '-',
			flags & VM_WRITE ? 'w' : '-',
			flags & VM_EXEC ? '-' : '-',
			flags & VM_MAYSHARE ? 's' : 'p',
			pgoff, MAJOR(dev), MINOR(dev), ino);
}

"""
                marker = "static void\nshow_map_vma("
                if marker in content:
                    content = content.replace(marker, fake_func + marker, 1)
                    logger.info("  task_mmu.c: 已插入 show_vma_header_prefix_fake")

            # 2. 插入 lineage/jit-zygote-cache 检测（独立检查，仅一次）
            if "sukisu_file" not in content and "#endif // #ifdef CONFIG_KSU_SUSFS_SUS_KSTAT" in content:
                if fb in ["android14-6.1", "android13-5.15", "android12-5.10"]:
                    detection_code = "\t\tstruct file *sukisu_file = vma->vm_file;\n\t\tif (sukisu_file && sukisu_file->f_path.dentry) {\n\t\t\tconst char *sukisu_path = sukisu_file->f_path.dentry->d_name.name;\n\t\t\tif (strstr(sukisu_path, \"lineage\")) {\n\t\t\t\tshow_vma_header_prefix(m, vma->vm_start, vma->vm_end,\n\t\t\t\t\t\tflags, pgoff, dev, ino);\n\t\t\t\tseq_puts(m, \"/system/framework/framework-res.apk\");\n\t\t\t\tseq_putc(m, '\\n');\n\t\t\t\treturn;\n\t\t\t}\n\t\t\tif (strstr(sukisu_path, \"jit-zygote-cache\")) {\n\t\t\t\tshow_vma_header_prefix_fake(m, vma->vm_start, vma->vm_end,\n\t\t\t\t\t\tflags, pgoff, dev, ino);\n\t\t\t}\n\t\t}\n"
                else:
                    detection_code = "\t\tstruct dentry *sukisu_dentry = file->f_path.dentry;\n\t\tif (sukisu_dentry) {\n\t\t\tconst char *sukisu_path = sukisu_dentry->d_name.name;\n\t\t\tif (strstr(sukisu_path, \"lineage\")) {\n\t\t\t\tshow_vma_header_prefix(m, vma->vm_start, vma->vm_end,\n\t\t\t\t\t\tflags, pgoff, dev, ino);\n\t\t\t\tseq_puts(m, \"/system/framework/framework-res.apk\");\n\t\t\t\tseq_putc(m, '\\n');\n\t\t\t\treturn;\n\t\t\t}\n\t\t\tif (strstr(sukisu_path, \"jit-zygote-cache\")) {\n\t\t\t\tshow_vma_header_prefix_fake(m, vma->vm_start, vma->vm_end,\n\t\t\t\t\t\tflags, pgoff, dev, ino);\n\t\t\t}\n\t\t}\n"

                pattern = r'(\t*#[ ]*endif[ ]*//[ ]*#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT\s*\n)'
                # Skip first match (file-scope extern declaration), match second one (inside show_map_vma)
                matches = list(re.finditer(pattern, content))
                if len(matches) >= 2:
                    insert_pos = matches[1].start()
                elif len(matches) == 1:
                    insert_pos = matches[0].start()
                else:
                    logger.warning("  SUS_KSTAT #endif not found, skip detection code")
                    insert_pos = None
                if insert_pos is not None:
                    content = content[:insert_pos] + detection_code + content[insert_pos:]
                logger.info("  task_mmu.c: 已插入 lineage/jit-zygote-cache 检测")

            # 3. 确保 <linux/string.h> 已包含（strstr 需要）
            if "#include <linux/string.h>" not in content:
                content = content.replace("#include <linux/sched/mm.h>",
                                          "#include <linux/sched/mm.h>\n#include <linux/string.h>")
                logger.info("  task_mmu.c: 已添加 string.h 头文件")

            with open(task_mmu, "w") as f:
                f.write(content)

        # ===== base.c: mm_find_next_nonspec_vma =====
        base_c = Path("fs/proc/base.c")
        if base_c.exists():
            with open(base_c, "r") as f:
                base_content = f.read()

            if "lineage" not in base_content and "mm_find_next_nonspec_vma" in base_content:
                orig = "\tif (vma && vma->vm_file) {\n\t\t*path = vma->vm_file->f_path;\n\t\tpath_get(path);\n\t\trc = 0;\n\t}"
                new = "\tif (vma) {\n\t\tif (vma->vm_file) {\n\t\t\tif (strstr(vma->vm_file->f_path.dentry->d_name.name, \"lineage\")) {\n\t\t\t\trc = kern_path(\"/system/framework/framework-res.apk\", LOOKUP_FOLLOW, path);\n\t\t\t} else {\n\t\t\t\t*path = vma->vm_file->f_path;\n\t\t\t\tpath_get(path);\n\t\t\t\trc = 0;\n\t\t\t}\n\t\t}\n\t}"
                if orig in base_content:
                    base_content = base_content.replace(orig, new)
                    with open(base_c, "w") as f:
                        f.write(base_content)
                    logger.info("  base.c: 已添加 lineage 路径检测")
                else:
                    logger.warning("  base.c: 原始模式不匹配，跳过")

    def apply_zram_patches(self):
        if not self.config.use_zram:
            return
        logger.info("=== 应用 ZRAM (LZ4KD) 补丁 ===")
        self._chdir(self.work_dir / "common")
        for src in [
            (self.sukisu_patch_dir / "other/zram/lz4k/include/linux", "include/linux/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/lib", "lib/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/crypto", "crypto/"),
            (self.sukisu_patch_dir / "other/zram/lz4k_oplus", "lib/"),
        ]:
            if src[0].exists():
                self._run_cmd(f"cp -r {src[0]}/* {src[1]}", check=False)
        zram_patch_dir = self.sukisu_patch_dir / f"other/zram/zram_patch/{self.config.kernel_version}"
        for patch in ["lz4kd.patch", "lz4k_oplus.patch"]:
            p = zram_patch_dir / patch
            if p.exists():
                self._run_cmd(f"patch -p1 -F 3 < {p}", check=False)

    def apply_task_mmu_fixes(self):
        logger.info("=== 应用 task_mmu.c 修复 ===")
        self._chdir(self.work_dir / "common")
        task_mmu = Path("fs/proc/task_mmu.c")
        if not task_mmu.exists():
            return

        fb = f"{self.config.android_version}-{self.config.kernel_version}"
        with open(task_mmu, "r") as f:
            content = f.read()

        if fb == "android15-6.6" and "unsigned int nr_subpages" not in content:
            self._fix_base_c_header()
        elif fb == "android14-6.1" and "if (!vma_pages(vma))" not in content:
            self._fix_base_c_header()
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)
        # 修复 SUSFS/SukiSU 补丁引入的 dentry 未初始化问题
        if "spoofed_redirected_name" in content and "struct dentry *dentry;" in content:
            if "struct dentry *dentry = NULL;" not in content:
                content = content.replace("struct dentry *dentry;", "struct dentry *dentry = NULL;")
                with open(task_mmu, "w") as f:
                    f.write(content)
                logger.info("  已修复 dentry 未初始化问题")
        elif fb in ["android12-5.10", "android13-5.10", "android13-5.15"] and "if (!vma_pages(vma))" not in content:
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)

    def _fix_base_c_header(self):
        base_c = self.work_dir / "common/fs/proc/base.c"
        if not base_c.exists():
            return
        with open(base_c, "r") as f:
            content = f.read()
        if "#include <linux/dma-buf.h>" not in content:
            content = content.replace("#include <linux/cpufreq_times.h>",
                                    "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>")
            with open(base_c, "w") as f:
                f.write(content)

    def configure_kernel(self):
        logger.info("=== 配置内核 ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning(f"配置文件不存在: {config_file}")
            return

        with open(config_file, "a") as f:
            f.write(self.KERNEL_CONFIG_TEMPLATE)
            if self.config.kernel_version != "6.6":
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=y\n")
            else:
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=n\n")

        if self.config.use_zram:
            self._configure_zram()
            self._configure_bazel()

        if self.config.set_default_bbr:
            with open(config_file, "a") as f:
                f.write("CONFIG_DEFAULT_BBR=y\n")

        build_config = self.work_dir / "common/build.config.gki"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("check_defconfig", "")
            with open(build_config, "w") as f:
                f.write(content)

    def _configure_zram(self):
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "r") as f:
            content = f.read()
        kv = self.config.kernel_version
        if kv == "5.10":
            with open(config_file, "a") as f:
                f.write(self.ZRAM_CONFIG_5_10)
        else:
            content = content.replace("CONFIG_ZRAM=m", "CONFIG_ZRAM=y")
            with open(config_file, "w") as f:
                f.write(content)
            with open(config_file, "a") as f:
                f.write("CONFIG_ZSMALLOC=y\n")
        with open(config_file, "a") as f:
            f.write(self.ZRAM_CONFIG_COMMON)

    def _configure_bazel(self):
        modules_bzl = self.work_dir / "common/modules.bzl"
        if modules_bzl.exists():
            with open(modules_bzl, "r") as f:
                content = f.read()
            modified = False
            for old in ['"drivers/block/zram/zram.ko",\n', '"drivers/block/zram/zram.ko",',
                       '"mm/zsmalloc.ko",\n', '"mm/zsmalloc.ko",']:
                if old in content:
                    content = content.replace(old, '')
                    modified = True
            if modified:
                with open(modules_bzl, "w") as f:
                    f.write(content)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "a") as f:
            f.write("CONFIG_MODULE_SIG_FORCE=n\n")

    def configure_kernel_name(self):
        logger.info("=== 配置内核名称 ===")
        self._chdir(self.work_dir)
        MAX_CUSTOM_LEN = 48
        safe_custom_version = ""
        if self.config.custom_version:
            safe_custom_version = self.config.custom_version.rstrip('-')[:MAX_CUSTOM_LEN]

        setlocalversion = self.work_dir / "common/scripts/setlocalversion"
        if setlocalversion.exists():
            with open(setlocalversion, "r") as f:
                content = f.read()
            if safe_custom_version:
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if 'echo "$res"' in line and not line.strip().startswith('#'):
                        lines[i] = f'\techo "{safe_custom_version}$res"'
                        break
                with open(setlocalversion, "w") as f:
                    f.write('\n'.join(lines))
            if "-dirty" in content:
                content = content.replace("-dirty", "")
                with open(setlocalversion, "w") as f:
                    f.write(content)

        import datetime
        current_time = datetime.datetime.now(datetime.timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            with open(mkcompile_h, "r") as f:
                content = f.read()
            content = content.replace('UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                                    f'UTS_VERSION="#1 SMP PREEMPT {current_time}"')
            with open(mkcompile_h, "w") as f:
                f.write(content)

        if self.config.kernel_version in ["6.1", "6.6"]:
            init_makefile = self.work_dir / "common/init/Makefile"
            if init_makefile.exists():
                with open(init_makefile, "r") as f:
                    content = f.read()
                content = content.replace('$(preempt-flag-y) "$(build-timestamp)"', f'$(preempt-flag-y) "{current_time}"')
                with open(init_makefile, "w") as f:
                    f.write(content)

        if not (self.work_dir / "build/build.sh").exists():
            bazel_build = self.work_dir / "common/BUILD.bazel"
            if bazel_build.exists():
                with open(bazel_build, "r") as f:
                    content = f.read()
                lines = [l for l in content.split('\n') if '"protected_exports_list"' not in l or 'android/abi_gki_protected_exports_aarch64' not in l]
                with open(bazel_build, "w") as f:
                    f.write('\n'.join(lines))

            abi_path = self.work_dir / "common/android/abi_gki_protected_exports_aarch64"
            if abi_path.exists():
                import shutil
                try:
                    if abi_path.is_dir():
                        shutil.rmtree(abi_path)
                    else:
                        abi_path.unlink()
                except Exception:
                    pass

            stamp_bzl = self.work_dir / "build/kernel/kleaf/impl/stamp.bzl"
            if stamp_bzl.exists():
                with open(stamp_bzl, "r") as f:
                    content = f.read()
                content = content.replace("-maybe-dirty", "")
                with open(stamp_bzl, "w") as f:
                    f.write(content)

            if self.config.custom_version:
                config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
                if config_file.exists():
                    with open(config_file, "r") as f:
                        content = f.read()
                    content = re.sub(r'^CONFIG_LOCALVERSION=".*"$', f'CONFIG_LOCALVERSION="{self.config.custom_version}"', content, flags=re.MULTILINE)
                    with open(config_file, "w") as f:
                        f.write(content)
                else:
                    logger.warning(f"配置文件不存在，跳过 custom_version 设置: {config_file}")

    def show_kernel_config(self):
        logger.info("=== 显示内核配置列表 ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        
        if not config_file.exists():
            logger.warning(f"配置文件不存在: {config_file}")
            return
        
        with open(config_file, "r") as f:
            lines = f.readlines()
        
        config_lines = [line.strip() for line in lines if line.strip().startswith("CONFIG_")]
        
        key_configs = {
            "CONFIG_KSU": "KernelSU",
            "CONFIG_KPM": "KPM",
            "CONFIG_KSU_SUSFS": "SUSFS",
            "CONFIG_BBG": "Baseband-guard",
            "CONFIG_BBR": "BBR",
            "CONFIG_ZRAM": "ZRAM",
        }
        
        logger.info("关键配置状态:")
        for prefix, name in key_configs.items():
            found = [c for c in config_lines if c.startswith(prefix)]
            if found:
                status = "已启用"
            else:
                status = "未配置"
            logger.info(f"  [{status}] {name}")
            if found:
                for f in sorted(found):
                    logger.info(f"      -> {f}")
        
        # 显示 ZRAM 相关配置
        if self.config.use_zram:
            zram_configs = [c for c in config_lines if any(x in c for x in ["ZRAM", "ZSMALLOC", "LZ4", "LZ4KD", "CRYPTO_LZ4", "MODULE_SIG"])]
            if zram_configs:
                logger.info("ZRAM 相关配置:")
                for zc in sorted(zram_configs):
                    logger.info(f"  -> {zc}")
        
        logger.info("-" * 60)

    def build_kernel(self) -> bool:
        logger.info("=== 开始编译内核 ===")
        self._chdir(self.work_dir)

        build_config = self.work_dir / "common/build.config.gki.aarch64"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            lines = [l for l in content.split('\n') if 'MODULES_ORDER=android/gki_aarch64_modules' not in l and 'KMI_SYMBOL_LIST_STRICT_MODE' not in l]
            with open(build_config, "w") as f:
                f.write('\n'.join(lines))

        try:
            if (self.work_dir / "build/build.sh").exists():
                logger.info("使用旧版构建方式...")
                result = self._run_cmd("LTO=thin BUILD_CONFIG=common/build.config.gki.aarch64 build/build.sh CC=\"/usr/bin/ccache clang\"", check=False)
            else:
                logger.info("使用 Bazel 构建方式...")
                result = self._run_cmd("tools/bazel build --disk_cache=/home/runner/.cache/bazel --config=fast --lto=thin //common:kernel_aarch64_dist", check=False)

            if result.returncode == 0:
                logger.info("=== 内核编译成功 ===")
                return True
            logger.error(f"内核编译失败: {result.stderr if result.stderr else 'Unknown error'}")
            return False
        except Exception as e:
            logger.error(f"编译过程出错: {e}")
            return False

    def patch_kpm_image(self):
        if not self.config.use_kpm or self.config.kernel_version == "6.6":
            return
        logger.info("=== 修补 Image 文件 (KPM) ===")
        self._chdir(self.work_dir)

        if self.config.android_version in ["android12", "android13"]:
            image_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_dir = self.work_dir / "bazel-bin/common/kernel_aarch64"

        if not image_dir.exists():
            return
        self._chdir(image_dir)
        self._run_cmd(f"curl -LSs {KPM_PATCH_URL} -o patch && chmod 777 patch && ./patch", check=False)
        if (image_dir / "oImage").exists():
            self._run_cmd("mv oImage Image", check=False)

    def prepare_boot_images(self) -> list:
        logger.info("=== 准备启动镜像 ===")
        self._chdir(self.work_dir)
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        artifacts = []

        if self.config.android_version in ["android12", "android13"]:
            image_source = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_source = self.work_dir / "bazel-bin/common/kernel_aarch64"

        for image_name in ["Image", "Image.lz4"]:
            src = image_source / image_name
            if src.exists():
                self._run_cmd(f"cp {src} {bootimgs_dir}/ && cp {src} {self.work_dir}/", check=False)

        if (self.work_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)

        if self.config.android_version == "android12":
            self._prepare_android12_boot_images(bootimgs_dir, artifacts)
        else:
            self._prepare_boot_images_generic(bootimgs_dir, artifacts)
        return artifacts

    def _prepare_android12_boot_images(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        gki_url = f"https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-{self.config.os_patch_level}_{self.config.revision}.zip"
        fallback_url = "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-2023-01_r1.zip"
        result = subprocess.run(f"curl -sL -w '%{{http_code}}' {gki_url} -o /dev/null", shell=True, capture_output=True, text=True)
        url = gki_url if "200" in result.stdout else fallback_url
        self._run_cmd(f"curl -Lo gki-kernel.zip {url} && unzip -o gki-kernel.zip && rm gki-kernel.zip", check=False)
        boot_img_path = bootimgs_dir / "boot-5.10.img"
        if boot_img_path.exists():
            self._run_cmd(f"$UNPACK_BOOTIMG --boot_img={boot_img_path}", check=False)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)

    def _prepare_boot_images_generic(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=False)

    def _create_boot_image_variants(self, bootimgs_dir: Path, artifacts: list, has_ramdisk: bool = False):
        self._chdir(bootimgs_dir)
        if (bootimgs_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)

        for kernel_file, output_file in [("Image", "boot.img"), ("Image.gz", "boot-gz.img"), ("Image.lz4", "boot-lz4.img")]:
            kernel_path = bootimgs_dir / kernel_file
            if not kernel_path.exists():
                continue
            cmd = f"$MKBOOTIMG --header_version 4 --kernel {kernel_file} --output {output_file}"
            if has_ramdisk:
                cmd += f" --ramdisk out/ramdisk --os_version 12.0.0 --os_patch_level {self.config.os_patch_level}"
            self._run_cmd(cmd, check=False)
            self._run_cmd(f"$AVBTOOL add_hash_footer --partition_name boot --partition_size $((64 * 1024 * 1024)) --image {output_file} --algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH", check=False)
            dest = self.work_dir / f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-{output_file}"
            self._run_cmd(f"cp {output_file} {dest}", check=False)
            artifacts.append(str(dest))

    def create_anykernel_zips(self) -> list:
        logger.info("=== 创建 AnyKernel3 ZIP 文件 ===")
        self._chdir(self.work_dir)
        artifacts = []
        ak3_dir = self.anykernel_dir

        for suffix in ["", "-lz4", "-gz"]:
            image_file = f"Image{suffix}"
            image_path = self.work_dir / image_file
            if not image_path.exists():
                continue
            zip_name = f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-AnyKernel3{suffix}.zip"
            self._run_cmd(f"cp {image_path} {ak3_dir}/", check=False)
            self._chdir(ak3_dir)
            self._run_cmd(f"zip -r ../{zip_name} ./*", check=False)
            self._run_cmd(f"rm {ak3_dir}/{image_file}", check=False)
            artifacts.append(str(self.work_dir / zip_name))
            self._chdir(self.work_dir)
        return artifacts

    def build(self) -> BuildResult:
        import time
        start_time = time.time()
        logger.info("=" * 50)
        logger.info(f"开始 GKI Kernel 构建 - {self.config.config_name}")
        logger.info("=" * 50)

        try:
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.add_kernel_supatch()
            self.add_kernelsu()
            self.add_bbg()
            self.apply_susfs_patches()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()

            if not self.build_kernel():
                return BuildResult(success=False, config=self.config, message="内核编译失败", build_time=time.time() - start_time)

            self.patch_kpm_image()
            artifacts = []
            artifacts.extend(self.prepare_boot_images())
            artifacts.extend(self.create_anykernel_zips())

            build_time = time.time() - start_time
            logger.info(f"构建成功! 耗时: {build_time:.2f} 秒, 生成 {len(artifacts)} 个产物")
            return BuildResult(success=True, config=self.config, message="构建成功", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.error(f"构建过程出错: {e}")
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.time() - start_time)
