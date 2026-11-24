# CronPulse

**Read your cron schedule before your cron schedule reads you.**

[![CI](https://github.com/stackpulse-lab/cronpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/stackpulse-lab/cronpulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

CronPulse is a zero-dependency command line tool that explains cron
expressions, previews the next run times, and catches schedules that will
**never fire**. It is built for operators who would rather find a typo on a
laptop than at 03:00 on a Sunday.

```console
$ cronpulse next "0 9 * * 1-5" -n 3
0 9 * * 1-5  (after 2026-11-24T17:49:00+08:00)
 1. 2026-11-25 09:00:00+08:00
 2. 2026-11-26 09:00:00+08:00
 3. 2026-11-27 09:00:00+08:00

$ cronpulse check "0 0 30 2 *"
0 0 30 2 *: NEVER fires within 8 years
```

## Why

Cron syntax is small but full of traps:

- `0 0 30 2 *` is syntactically valid and can never run (February has 29 days at most).
- `0 0 31 4 *` looks fine until you notice April has 30 days.
- `0 0 1 * 1` does **not** mean "the first Monday of the month" — classic cron
  ORs the day fields, so it runs on the 1st *and* on every Monday.
- `0 9 * * MON-FRI` silently uses the server's timezone, not yours.

CronPulse makes those cases visible: expand the fields, print concrete
timestamps in your timezone, and fail CI when a schedule cannot run.

## Install

Python 3.9+ and nothing else. From a checkout:

```console
python -m pip install .
cronpulse --version
```

Or run it without installing:

```console
PYTHONPATH=src python -m cronpulse next "*/15 * * * *"
```

## Usage

### Preview upcoming runs

```console
cronpulse next "*/15 9-18 * * 1-5" -n 5
cronpulse next "@daily" --tz Asia/Shanghai --after 2026-12-31T23:00
cronpulse next "0 0 29 2 *" -n 3        # leap-day only schedule
```

### Detect a schedule that never fires

```console
cronpulse check "0 0 31 4 *" --json
```

Exit codes are automation friendly:

| Code | Meaning |
| --- | --- |
| `0` | schedule fires; preview printed |
| `2` | invalid expression / bad arguments |
| `3` | schedule never fires inside the 8-year horizon |

Use it in CI or a pre-deploy script:

```console
cronpulse check "$SCHEDULE" || echo "schedule cannot fire, refusing to deploy"
```

### Explain how fields were parsed

```console
$ cronpulse explain "0 9 1 * 1-5"
expression : 0 9 1 * 1-5
fields     : minute=0; hour=9; day-of-month=1; month=*; day-of-week=1,2,3,4,5
note       : day-of-month and day-of-week are OR-ed (classic cron)
```

### DST boundaries

Schedules are wall-clock based, so a timezone's DST transition can make a
run vanish or happen twice. CronPulse maps every wall-clock time to real
instants and tells you what happened:

```console
$ cronpulse next "30 2 * * *" --tz America/New_York --after 2026-03-07T12:00 -n 2
30 2 * * *  (after 2026-03-07T12:00:00-05:00)
 1. 2026-03-09 02:30:00-04:00
 2. 2026-03-10 02:30:00-04:00
note: skipped 1 wall-clock time(s) that do not exist (DST gap): 2026-03-08 02:30:00
```

- **Gap (spring forward).** Times that do not exist are skipped, and reported
  as `note: skipped …`; `--json` exposes them under `dst.skipped`.
- **Overlap (fall back).** Times that happen twice are listed twice with their
  real offsets, and reported under `dst.ambiguous`. Both instants are
  chronological, so a `*/30 1 * * *` schedule prints 01:00-04:00, 01:30-04:00,
  01:00-05:00, 01:30-05:00.
- **Naive input is unchanged.** Without a timezone CronPulse keeps the old
  wall-clock behaviour, which is what plain crontab entries assume.

`cronpulse explain` also prints a short `CRON_TZ` hint: cron daemons run in the
system timezone (Vixie cron additionally honours a per-crontab
`CRON_TZ=<IANA zone>` line), and systemd timers ignore `CRON_TZ` entirely.

## Supported syntax

| Feature | Example | Notes |
| --- | --- | --- |
| Wildcard | `*` | every value in the field |
| `?` | `0 0 ? * 1` | treated as `*` (Quartz-style input) |
| List | `0,30 8,20 * * *` | |
| Range | `0 9-17 * * *` | ascending only |
| Step | `*/15`, `5/20`, `9-17/4` | starts at the range start |
| Month names | `jan` … `dec` | case insensitive |
| Weekday names | `sun` … `sat` | case insensitive |
| Sunday | `0` or `7` | normalised to `0` |
| Macros | `@hourly` `@daily` `@weekly` `@monthly` `@yearly` | `@annually`, `@midnight` aliases |
| Quartz `L` `W` `#` | `0 0 L * *`, `0 0 15W * *`, `0 0 * * MON#2` | **rejected** with an explicit dialect error (see below) |

Fields: `minute hour day-of-month month day-of-week` (5 fields).

Quartz extensions (`L`, `LW`, `L-3`, `15W`, `6L`, `MON#2`) have no classic
cron equivalent, so CronPulse refuses them instead of guessing:

```console
$ cronpulse check "0 0 L * *"
cronpulse: invalid expression: day-of-month: Quartz extension `L` (last day
of month) in 'L' is not supported; cronpulse implements the classic 5-field
Vixie cron dialect. Write out the concrete dates, or keep this rule in a
Quartz-based scheduler.
```

Exit code is `2`, the same as any other invalid expression, so scripts can
gate on it.

### Semantics worth knowing

- **Day-of-month / day-of-week OR rule.** When both fields are restricted,
  a day matches if *either* field matches. CronPulse implements this classic
  behaviour and prints a note in `explain`.
- **Strictly after.** `next` returns occurrences strictly greater than
  `--after`, so passing the timestamp of a run never echoes that run back.
- **Timezone.** `--tz Asia/Shanghai` applies to `--after` and output. Without
  it, the host's local timezone is used and printed with its offset.
- **DST-aware preview.** With a DST timezone, times inside a spring-forward
  gap are skipped and reported; times inside a fall-back overlap are returned
  twice with their distinct offsets. `--json` exposes both under `dst`.
- **Never-fires horizon.** Detection scans 8 years, which covers a full
  leap-year cycle (e.g. `0 0 29 2 *` is correctly reported as firing next on
  2028-02-29).

## Development

```console
git clone https://github.com/stackpulse-lab/cronpulse
cd cronpulse
python run_tests.py          # standard library unittest, no pytest needed
```

Layout:

```
src/cronpulse/expr.py     parser + CronExpr + next-run math
src/cronpulse/cli.py      argparse CLI
tests/                    parser, schedule and CLI tests
```

Design constraints: standard library only, no network access, no state on
disk, deterministic output.

## Roadmap

- [x] v0.1 — five-field parser, next-run preview, never-fires detection, CLI
- [x] v0.2 — DST-aware preview (gap/overlap notes) + `CRON_TZ` hint in `explain`
- [x] v0.3 — explicit Quartz `L`/`W`/`#` rejection with dialect guidance
- [ ] v0.4 — scheduled-drift report from systemd/crontab inventory

## License

MIT © 2026 StackPulse Lab

---

# CronPulse（中文）

**先读懂 cron，再让它替你看夜班。**

CronPulse 是一个零依赖命令行工具，用来看懂 cron 表达式、预览未来触发时间、
并检测**永远不会触发**的定时任务。适合在本地就发现凌晨三点才会暴露的配置错误。

```console
$ cronpulse check "0 0 30 2 *"
0 0 30 2 *: NEVER fires within 8 years
```

## 为什么需要

- `0 0 30 2 *` 语法合法，但 2 月最多 29 天，永远不可能执行。
- `0 0 31 4 *` 同理，4 月只有 30 天。
- `0 0 1 * 1` 不是「每月第一个周一」：经典 cron 的「日 / 星期」是**或**关系，
  它会在每月 1 号**以及**每个周一执行。
- `0 9 * * MON-FRI` 使用服务器时区，不一定是你以为的时区。

CronPulse 把字段展开、按指定时区打印真实时间戳，并让「永不触发」以退出码
`3` 暴露出来，可直接接入 CI。

## 使用

```console
cronpulse next "*/15 9-18 * * 1-5" -n 5
cronpulse next "@daily" --tz Asia/Shanghai --after 2026-12-31T23:00
cronpulse check "0 0 31 4 *" --json
cronpulse explain "0 9 1 * 1-5"
```

退出码：`0` 正常；`2` 表达式或参数非法；`3` 8 年窗口内不会触发。

## 支持的语法

5 字段：`分 时 日 月 周`。支持 `*`、`?`（视作 `*`）、列表、升序范围、步长、
月份/星期英文缩写、周日 `0` 或 `7`，以及 `@hourly` `@daily` `@weekly`
`@monthly` `@yearly` 等宏。

Quartz 专有写法（`L`、`LW`、`L-3`、`15W`、`6L`、`MON#2`）在经典 cron 中
没有等价语义，因此**直接报错并说明原因**，退出码 `2`，不会静默忽略或近似：

行为说明：

- 日与星期同时受限时按经典 cron 的**或**语义匹配；
- Quartz `L`/`W`/`#` 会得到明确错误提示（含字段名、含义与替代建议）；
- `next` 严格取 `--after` 之后的时间，不会回显当前这一分钟；
- `--tz` 同时作用于输入与输出；省略时使用主机本地时区；
- 永不触发检测扫描 8 年，覆盖完整闰年周期（`0 0 29 2 *` 下次为 2028-02-29）。

### 夏令时（DST）边界

cron 按本地挂钟时间匹配，时区切换会让某些任务**消失**或**执行两次**。
CronPulse 会把每个挂钟时间映射成真实时刻并明确提示：

```console
$ cronpulse next "30 2 * * *" --tz America/New_York --after 2026-03-07T12:00 -n 2
30 2 * * *  (after 2026-03-07T12:00:00-05:00)
 1. 2026-03-09 02:30:00-04:00
 2. 2026-03-10 02:30:00-04:00
note: skipped 1 wall-clock time(s) that do not exist (DST gap): 2026-03-08 02:30:00
```

- **春季跳过**：不存在的时刻直接跳过并提示，`--json` 中位于 `dst.skipped`。
- **秋季重叠**：出现两次的时刻会带真实偏移各列一次，位于 `dst.ambiguous`，
  且严格按真实时间排序（`*/30 1 * * *` 会输出 01:00-04:00、01:30-04:00、
  01:00-05:00、01:30-05:00）。
- **无时区输入行为不变**：与朴素的 crontab 挂钟语义一致，便于对照。

`cronpulse explain` 还会给出 `CRON_TZ` 提示：cron 守护进程使用系统时区
（Vixie cron 支持在 crontab 顶部写 `CRON_TZ=<IANA 时区>`），systemd timer
则不认 `CRON_TZ`。

## 开发

```console
python run_tests.py     # 仅用标准库 unittest
```

设计约束：零第三方依赖、不联网、不落盘、输出确定。MIT 许可。
