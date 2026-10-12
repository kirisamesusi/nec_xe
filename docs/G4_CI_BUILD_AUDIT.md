# G4-CI：WhateverXe macOS 原生编译配置审计

初始审计：2026-10-12；最新验证：2026-10-13（Asia/Shanghai）。状态：G4-CI原生编译、默认CPU资格和产物检查PASS。下文首次运行/修复候选段落保留当时事实，当前结果以此处第二次运行证据为准。

第二次运行[38277643192](https://github.com/kirisamesusi/nec_xe/actions/runs/38277643192)，CI源码commit`0adf5b6301c56594416933c98960878a7ff92989`：环境、固定依赖、原样build.py、原样test.py出口、输入完整性、两个kext产物资格及打包全部PASS。原runner结果54个stem项全0；独立复核两组同名任务后得到56个语言限定默认入口全0（34C+++22Python，含source guards）。无exact donor replay/内核加载/NEC启动或内屏实测。

本地对下载binary另用LLVM readobj/nm复核了x86_64/MH_KEXT_BUNDLE、load-command边界、生命周期符号和禁止imports；ZIP七文件（两bundle共四文件+三许可证）CRC/逐bytes比对PASS。GitHub artifact SHA256`a7c075951a3a8ab85c70ba21ab772e7903e423e8ff92fc6f9ed385a40429e5f1`匹配，81项pipeline文件hash全部通过。`kexts.zip` SHA256为`966bc72d54502430ec6d14facd724c14fa5ff5a72506a945399098f94b482154`。

| 产物 | 版本 | 架构 | binary SHA-256 |
|---|---|---|---|
| WhateverXe.kext | 0.2.3 | x86_64 | `e2da78c58791bf6211c6bb61a596769fe496c3db6afd07139e212e2c12f49227` |
| AppleIntelTGLCompat.kext | 0.5.0 | x86_64 | `34aabaead6c7bb5fb62b4e821fb71ddbea6e9184c6cf839bc8f6ab9b057787b4` |

实际主机仍为Intel macOS15.7.9/24G830、Xcode16.4/16F6、AppleClang17.0.0、SDK15.5、Python3.12.10。驱动运行目标仍为20G1443。锁定输入/工具链与可重复操作流程已有实测，两个运行报告的binary hash不同，不宣称字节级可重复构建。首次FAIL日志保留，源/pin/EXP015016/核心采样不改；G0及内屏验收状态不提升。用户授权的两次运行均已结束，不自动第三次。

2026-10-13更新：首次手动运行38275645396（配置commit87d47a3）整体FAIL；工具链/依赖资格与原生build.py编译链接PASS，但CPU结果54个命名项中1个source-guard PASS、53个因标准头文件发现失败，产物检查FAIL。首次实际二进制未留存，不能事后确认格式或可加载性。已准备本地CI修复，18人工测试PASS；修复尚未推送、未云端验证，也未启动第二次运行。原生BUILD从缺环境阻塞细化为“编译命令PASS / 产物验收未通过”。

本次用户要求的“G4 编译环境”记为 G4-CI。既有规格中的 G4 是 macOS 最小启动，保留原含义、编号和 NOT_TESTED 状态。CI 是解除 G3 构建阻塞的开发设施；G0 恢复 Gate 与内屏验收不因 CI 配置通过而改变。

## 基线和依赖

只编译 WhateverXe 锁定 commit `61a449255d385aeb468f64e5d5042e126f387b90`，不使用 main/latest，不应用 NEC EXP-016 草稿。本地源码、EXP-015/016 实验证据和 NEC 配置均未修改。

该 commit 的 [上游 CI](https://github.com/b00t0x/WhateverXe/blob/61a449255d385aeb468f64e5d5042e126f387b90/.github/workflows/ci.yml) 已指定 MacKernelSDK commit 与 Lilu 1.7.0 DEBUG。本流水线沿用这组依赖；调查中下载的较新版本只是初步审计输入，没有成为交付配置。

| 输入 | 固定版本 | 归档 SHA-256 |
|---|---|---|
| WhateverXe | `61a449255d385aeb468f64e5d5042e126f387b90` | `c0103dafd8a54ebf7d6f6bb24c41594927f53497864c2e3c9f99086ac287b880` |
| MacKernelSDK | `05094e5e88cec7caedbfb35e8449ed0db94bf95b` | `b6abad54ab347028d7c5495a105cdd59736bcecf12ab6bbb44df7041715994bd` |
| Lilu DEBUG | `1.7.0`；tag commit `f32ae0f11d10857f4b4c0f03440bca7c2e2a1793` | `2adf2425286aba87e9b50c6992724be057a89caa5884464b1adc00154c2fd012` |

完整 URL、字节数、版本和布局见 `.ci/dependencies.lock.json`。下载前不执行依赖代码，下载后先验证长度/SHA-256，随后检查 ZIP CRC、路径、重复名称和链接，再校验关键开发文件以及 Lilu bundle version。任何不匹配都失败，不自动更新 hash 或切到最新版本。

Lilu 1.7.0 的下载 hash 与锁定 WhateverXe CI 内的值一致，Release API 对该旧 asset 没有 digest。MacKernelSDK/WhateverXe hash 来自本次官方 codeload 下载；WhateverXe 的 181 个文件与禁用 CRLF 转换后的本地锁定 Git archive 全部相同。Hash 固定完整性，不等于独立签名认证；GPG/签名验证 NOT_TESTED。GitHub 重生成 ZIP 如改变字节，会失败并要求重新审计。

## 编译要求与工具链

锁定 [README](https://github.com/b00t0x/WhateverXe/blob/61a449255d385aeb468f64e5d5042e126f387b90/README.md) 要求 macOS、Xcode CLI、Python 3.9+、MacKernelSDK 和 Lilu 开发头文件/库。锁定源码没有规定精确 Xcode/clang 版本；本项目选择如下实验基线，兼容性尚待运行证明：

- runner：`macos-15-intel`，并检查运行时 `Darwin/x86_64`。
- Xcode：`16.4 / 16F6`，通过 job 的 `DEVELOPER_DIR` 选择，不运行 sudo/xcode-select，不自动降级。
- 系统 SDK：`macosx15.5`，只做工具链核对。kext 的 kernel headers 和 `libkmod.a` 来自固定 MacKernelSDK，并非把系统 SDK 或 runner macOS 15 当成驱动运行目标。
- Python：`3.12.10 / x64`，固定 setup-python action，不使用 pip/Homebrew 依赖。
- CPU fixture 的 `clang++` PATH 指向同一 Xcode Apple compiler，保存实际 clang/ld/OS/image 信息。

GitHub [runner 文档](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) 列出该 Intel label；[macOS 15 镜像清单](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-Readme.md) 列出 Xcode 16.4 与 SDK 15.5。托管镜像会更新，因此这是固定输入和工具链选择、记录实际环境的可复用流程，不宣称字节级可重复构建。

[build.py](https://github.com/b00t0x/WhateverXe/blob/61a449255d385aeb468f64e5d5042e126f387b90/tools/build.py) 使用 C++17、xcrun clang++/ld、x86_64、`-nostdinc`、MacKernelSDK Headers/Library，生成 WhateverXe 0.2.3 与 AppleIntelTGLCompat 0.5.0。WhateverXe 特意保留 10.6 deployment target、旧 kernel C++ ABI、`-fapple-kext`、`-no_fixup_chains`，并检查 lifecycle 符号及禁止的 userspace/DriverKit imports。这些参数不能为消除新版编译错误而随意改为 10.15/15 或删除断言。

Lilu 必须使用包含 `Contents/Resources/Headers` 和 `Library/plugin_start.cpp` 的 DEBUG 开发包。WhateverXe build Info.plist 的 Lilu 最低 runtime 声明为 1.2.0，不是本次开发依赖选型；固定开发包为上游 CI 的 1.7.0。

## 流水线行为

`.github/workflows/macos-build.yml` 只有 `workflow_dispatch`，没有 push/PR/schedule。所有三个 Actions 均固定完整 commit；token 权限为 contents:read，checkout 不保留凭据，仅稀疏获取 `.ci` 与该 workflow。

1. 固定工具链资格检查；不符则失败，记录错误。
2. 自动下载固定的三个归档，完整性与布局验证；保存所有输入文件的 SHA-256 清单。
3. 原样执行 `python tools/build.py --release --sdk ... --lilu ...`。
4. 原样执行 `python tools/test.py`，包括 34 C++ 与 22 Python 默认入口。普通编译返回非零时仍继续测试，构建与测试分别记结果；基础设施/超时异常保留失败状态，不伪造未执行步骤。
5. 核对56个语言限定的默认任务结果、所有返回值为整数0、输入文件未被修改。原上游runner将两个C++/Python同名任务合并，结果只有54个stem项且日志共名；修复保留原runner/原结果，并独立复核4个同名入口到不同日志，再与52个无冲突项构成56个资格结果。检查两个产物的Info.plist、lipo x86_64、Mach-O数值字段（magic/cputype/filetype=11）和上游build-manifest hash。
6. 只有 BUILD、CPU 和产物验证全部通过才生成 `kexts.zip`。失败不发布 kext 包，保留错误/已有测试结果/构建日志。
7. `always()` 上传独立新建的 `ci-output/`，保留 14 天；仅包含本次云端构建资料，不扫描/上传工作区的 reports/artifacts/第三方 checkout。

保存内容：`build.log`、`tests.log`、逐fixture log/results、`cpu-qualified-results.json`、同名入口独立日志、`environment.json`、`dependencies.lock.json`、`input-manifests.json`、上游`build-manifest.json`（编译完成即保存）、`result.json`、`artifact-manifest.json`、`SHA256SUMS`、依赖许可证、成功时的`kexts.zip`。修复另保留`build-diagnostics/`中明确标UNVERIFIED的编译输出及实际otool/lipo检查日志，供失败诊断；不得据此判定已通过CPU/格式/加载验收。源码commit、CI commit、run ID、依赖版本和产物hash均记录。kext ZIP附带WhateverXe/MacKernelSDK/Lilu许可证，Lilu原文按固定commit/长度/hash获取，不纳入编译输入。取消或runner强制结束时不能保证上传完成；检查实际run/artifact是否存在。

无 exact Apple donor 输入，不下载 Apple 专有驱动，不执行 optional replay。默认 runner 中的 forcewake 无 donor 路径只是 source guards。macOS CPU PASS 不等于 donor replay、kernel 加载、NEC 内屏/加速或 FL 通过。产物为研究构建，未签名、未加载、未部署；AppleIntelTGLCompat 也不自动安装。

## 工作区隐私审计

原 `.gitignore` 已存在，现补强：reports/artifacts 整目录不例外、third_party、构建缓存/产物、原始采样格式、EFI/config.plist、私人目录、账户密钥和凭据等。调查/status/hardware 文档默认留本地，只有本审计文档经过单独审阅后允许提交。

Git 当前没有已跟踪文件、没有首个 commit、没有 remote；未运行 add/commit/push。`.gitignore` 不会撤销历史泄漏，也不防止 `git add -f`。首轮内容扫描发现 8 处旧文档私人账户路径以及 1 处采集脚本 UUID 形状（待人工判读）；没有把匹配内容复制到公开文档。旧证据按用户要求保留原样，不批量替换或清洗。

因此首次提交必须使用下面的六文件白名单，不能使用 `git add .` / `git add -A`：

```text
.gitignore
.github/workflows/macos-build.yml
.ci/dependencies.lock.json
.ci/native_build.py
tests/test_ci_native_build.py
docs/G4_CI_BUILD_AUDIT.md
```

本地 STATUS/UPSTREAM_PIN/CHANGELOG 和实验报告的更新留在本地。工作区完整候选扫描、路径/行号匹配记录、文件备份及待提交 SHA-256 清单放在被忽略的本地 reports。白名单在提交前再核对 staged diff 和扫描；白名单外资料另行审阅和授权。

拟首次 commit：`ci: add pinned WhateverXe native macOS build`。用户批准后才创建此本地 commit；远程完整 owner/nec-xe、仓库可见性及 push/建库/dispatch 授权均未确定。批准本地 commit 不自动包含远程操作。

## 本地验证与限制

| 检查 | 结果 |
|---|---|
| 固定上游文件/构建/测试/CI/AGENTS 审计 | PASS |
| 三个最终依赖归档 hash/长度/CRC/开发布局/Lilu 版本 | PASS（离线准备） |
| 下载源码与锁定 Git 内容逐文件一致 | PASS：181 文件；本地 checkout 是 CRLF，未改原文件 |
| actionlint 1.7.12 | PASS；官方 checksum 和 Release API digest 核验；未配置 shellcheck/pyflakes |
| 项目 CI 边界测试 | PASS：12 人工测试；测试中的模拟 PASS 不是原生 BUILD |
| ignore 边界 | PASS：18 路径探针；不代表任意私密内容已自动识别 |
| EXP-015/016、原采样、旧 patch 和锁定源完整性 | PASS：9,340 文件前后 hash 相同 |
| GitHub workflow 首次运行 / 原生 BUILD / 原样默认 CPU runner | NOT_TESTED；审批阻塞 |
| exact donor / NEC macOS 启动 / 内屏 / 加速 / FL | NOT_TESTED；原有阻塞保持 |

首轮 source/hash 审计因 Git CRLF 输出而 FAIL；修正比较方法后 PASS，初始日志保留。缺 PyYAML 未安装额外包，改用经过 checksum 验证的 actionlint；该工具通过不替代原生试跑。

## Xcode / SDK 错误处理与最小下一实验

假设最多三条：H1 固定上游依赖在 Xcode 16.4 可原样编译；H2 macOS 56 默认 fixture 可原样通过；H3 输入完整性、产物门槛和隔离上传能够拒绝不完整/失败产物。前两条只能由审批后的首次 dispatch 证伪；第三条已由人工边界测试有限验证。

下一低风险实验：获得明确授权后，提交白名单、核对完整远程位置和权限、获准推送，再单独批准一次手动 dispatch。运行应产生独立 artifact；读取 result.json、environment.json、build.log 和 fixture results 后，分别判 BUILD/CPU/产物 PASS/FAIL。云端工作不连接 NEC 硬件，不改 EFI/固件/系统卷。

失败特征与处理：

- Xcode 路径或 16F6/SDK15.5 不符：保留日志，不静默切换。根据实际 runner image 重新选可用的明确版本，更新 lock 与本审计后单变量重跑。
- missing kernel/Lilu header 或 plugin_start：先核对下载长度/hash、布局、include flags；不从随机镜像补头文件。
- linker 不支持 `-no_fixup_chains` / 老 deployment target、DriverKit vtable 或 fortify imports：定位第一条 compiler/linker 错误及 unresolved 符号。优先比较固定 Xcode 小版本，保留 ABI/身份 guards；若必须改脚本，在独立项目 patch 中解释并审阅，不覆盖锁定源树。
- sanitizer/mmap/stack 失败：保留失败 fixture 与环境信息，区分测试平台问题和源码缺陷；不删断言、关 sanitizer 或把 Windows 适配结果当 macOS PASS。

目前没有实际 Xcode/SDK 错误，不能给出已经生效的修复结论。停止/回退：取消云端 run，保留失败证据，停用新 workflow；本地文件恢复到报告中的修改前备份。没有 NEC 系统回滚步骤，因为本轮没有执行系统变更。

### 首次运行后的修复候选

初始审计时没有macOS实际错误；首次运行补充了实际失败证据，详见[run38275645396](https://github.com/kirisamesusi/nec_xe/actions/runs/38275645396)。固定Xcode16.4/16F6、AppleClang17.0.0、SDK15.5均可用，暂不升级工具链/依赖或更改driver ABI参数。

CPU全部记录失败项均为`cassert`/`string.h`等标准头文件未发现。修复在CPU测试及同名复核期间显式设置由`xcrun --sdk macosx --show-sdk-path`取得的SDKROOT，随后恢复原环境；kext编译环境和冻结上游源码不改。这是待第二次macOS运行验证的修复假设，[Clang Darwin工具链源码](https://github.com/llvm/llvm-project/blob/main/clang/lib/Driver/ToolChains/Darwin.cpp)支持SDKROOT作为默认isysroot。

原检查器要求otool文本`KEXT_BUNDLE`是本项目错误：[Apple otool源码](https://github.com/apple-oss-distributions/cctools/blob/main/otool/ofile_print.c)输出`KEXTBUNDLE`。修复直接检查薄Mach-O头的数值magic/cputype/filetype，保留格式诊断文本；测试覆盖合法格式、ARM/fat/错误类型/截断拒绝，以及文本不能伪造二进制格式。初始实际Mach-O未保留，因此不把修复解释为首轮产物已经验收成功。

同名任务复核保留原fixture、断言、ASan/UBSan参数与source hash，不减少默认任务、不掩盖失败，也不触发optional donor。18本地资格测试通过；编译后diag留存和artifact格式/CPU修复仍需新的单次云端实验。第二次dispatch未授权，本地修复可审阅，原首轮日志与结果保持原样。
