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

Fields: `minute hour day-of-month month day-of-week` (5 fields).

### Semantics worth knowing

- **Day-of-month / day-of-week OR rule.** When both fields are restricted,
  a day matches if *either* field matches. CronPulse implements this classic
  behaviour and prints a note in `explain`.
- **Strictly after.** `next` returns occurrences strictly greater than
  `--after`, so passing the timestamp of a run never echoes that run back.
- **Timezone.** `--tz Asia/Shanghai` applies to `--after` and output. Without
  it, the host's local timezone is used and printed with its offset.
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
- [ ] v0.2 — cron dialect hints (`CRON_TZ`, Quartz `L`/`W` warnings)
- [ ] v0.3 — scheduled-drift report from systemd/crontab inventory

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

行为说明：

- 日与星期同时受限时按经典 cron 的**或**语义匹配；
- `next` 严格取 `--after` 之后的时间，不会回显当前这一分钟；
- `--tz` 同时作用于输入与输出；省略时使用主机本地时区；
- 永不触发检测扫描 8 年，覆盖完整闰年周期（`0 0 29 2 *` 下次为 2028-02-29）。

## 开发

```console
python run_tests.py     # 仅用标准库 unittest
```

设计约束：零第三方依赖、不联网、不落盘、输出确定。MIT 许可。
