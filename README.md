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

### Check crontab drift

Declare the jobs you expect in a JSON manifest and compare it with the live
crontab:

```console
$ cat expectations.json
{
  "entries": [
    {"schedule": "@daily", "command": "/usr/local/bin/backup.sh --full"},
    {"schedule": "*/5 * * * *", "command": "/usr/local/bin/poll.sh"},
    {"schedule": "0 3 * * *", "command": "/usr/local/bin/report.sh"}
  ]
}

$ cronpulse drift /tmp/crontab --expect expectations.json
actual: 3 entries; expected: 3; matched: 1
unexpected (1):
  0 4 * * *  /usr/local/bin/oneoff.sh
changed (1):
  /usr/local/bin/backup.sh --full: 0 0 * * * -> 0 9 * * 1-5
```

- `--system` reads the user column used by `/etc/crontab` and `/etc/cron.d/*`;
  the user is part of the identity, so the same command under a different
  account is reported as missing + unexpected.
- `-` reads the crontab from standard input:
  `crontab -l | cronpulse drift - --expect expectations.json`.
- Schedules are compared semantically: `@daily` equals `0 0 * * *` and `1-5`
  equals `1,2,3,4,5`. Command whitespace is normalised; comments, blank lines
  and `NAME=value` environment lines are ignored.
- A command that exists on both sides with a different schedule is reported
  once as `changed`, not as one missing plus one unexpected entry.
- Duplicate entries and schedules that never fire are printed as warnings.
- Exit codes: `0` in sync, `4` drift or warnings, `2` unreadable or invalid
  input.

Use it as a deploy gate:

```console
crontab -l | cronpulse drift - --expect ops/expectations.json || exit 1
```

### Preview systemd `OnCalendar=` timers

systemd timers do not read cron: components are AND-ed, the weekday comes
first, and ranges use `..`. CronPulse previews the same specification:

```console
$ cronpulse calendar "Mon..Fri 09:00" -n 2 --after 2026-11-25T06:00
Mon..Fri 09:00  ->  Mon..Fri *-*-* 09:00:00  (after 2026-11-25T06:00:00+08:00)
 1. 2026-11-25 09:00:00+08:00
 2. 2026-11-26 09:00:00+08:00

$ cronpulse calendar quarterly -n 2 --after 2026-11-25T00:00
quarterly  ->  *-01,04,07,10-01 00:00:00  (after 2026-11-25T00:00:00)
 1. 2027-01-01 00:00:00
 2. 2027-04-01 00:00:00
```

- Shortcuts: `minutely` `hourly` `daily` `midnight` `weekly` `monthly`
  `quarterly` `semi-annually` `yearly` `annually`.
- Optional weekday prefix: `Mon`, `Mon..Fri`, `Sat,Sun` (full names work).
- Date `[YYYY-]MM-DD` and time `HH:MM[:SS]`; each component accepts `*`,
  `N`, `N..M`, `N/M`, `N..M/M` and comma lists. Month names are accepted.
- The normalised form is printed next to your input, so `weekly` shows that
  it becomes `Mon *-*-* 00:00:00`; `--json` exposes it as `normalized`.
- Weekday and day-of-month are **AND-ed**, unlike classic cron's OR rule.
- Exit code `3` when the specification cannot fire inside 8 years, e.g.
  `*-02-30 00:00` (February never has 30 days).
- Rejected with an explicit reason: the `~` last-day marker, the `?`
  wildcard, fractional seconds and trailing `UTC` / timezone suffixes
  (use `--tz`).
- DST follows the same policy as cron previews: nonexistent wall-clock times
  are skipped and noted, repeated times are listed once.

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
src/cronpulse/oncalendar.py systemd OnCalendar= parser + preview
src/cronpulse/manifest.py crontab manifest parsing + drift report
src/cronpulse/cli.py      argparse CLI
tests/                    parser, schedule, manifest and CLI tests
```

Design constraints: standard library only, no network access, no state on
disk, deterministic output.

## Roadmap

- [x] v0.1 — five-field parser, next-run preview, never-fires detection, CLI
- [x] v0.2 — DST-aware preview (gap/overlap notes) + `CRON_TZ` hint in `explain`
- [x] v0.3 — explicit Quartz `L`/`W`/`#` rejection with dialect guidance
- [x] v0.4 — crontab manifest drift check (semantic comparison, exit code 4)
- [x] v0.5 — systemd `OnCalendar=` preview (shortcuts, weekday prefix, steps, DST notes)
- [ ] v0.6 — systemd timer inventory drift check (`OnCalendar=` manifests)

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
cronpulse drift /etc/crontab --system --expect expectations.json
```

退出码：`0` 正常；`2` 表达式或参数非法；`3` 8 年窗口内不会触发；
`4` crontab 清单存在漂移或告警。

### 检查 crontab 漂移

把「应该存在的任务」写成 JSON 清单（顶层数组，或含 `entries` 数组的对象），
与线上 crontab 对比：

```console
$ cronpulse drift /tmp/crontab --expect expectations.json
actual: 3 entries; expected: 3; matched: 1
unexpected (1):
  0 4 * * *  /usr/local/bin/oneoff.sh
changed (1):
  /usr/local/bin/backup.sh --full: 0 0 * * * -> 0 9 * * 1-5
```

- `--system` 用于 `/etc/crontab`、`/etc/cron.d/*` 这类带用户列的清单；用户是
  任务身份的一部分，同一命令换了账号会同时报「缺失」与「多余」。
- 位置参数写 `-` 表示从标准输入读取：`crontab -l | cronpulse drift - --expect expectations.json`。
- 计划按语义比较：`@daily` 等同 `0 0 * * *`，`1-5` 等同 `1,2,3,4,5`；
  命令中的多余空白会被归一化；注释、空行与 `NAME=value` 环境行忽略。
- 同一命令的计划变化只报一条 `changed`，不会拆成「缺失 + 多余」。
- 重复条目与永不触发的计划会以 `warning:` 输出。
- 退出码：`0` 一致；`4` 存在漂移或告警；`2` 文件不可读或格式非法。

可直接作为部署门禁：

```console
crontab -l | cronpulse drift - --expect ops/expectations.json || exit 1
```

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

### 预览 systemd `OnCalendar=` 定时器

systemd timer 不读 cron：各组件是**与**关系，星期写在最前，范围用 `..`。
CronPulse 直接预览 systemd 使用的同一份表达式：

```console
$ cronpulse calendar "Mon..Fri 09:00" -n 2 --after 2026-11-25T06:00
Mon..Fri 09:00  ->  Mon..Fri *-*-* 09:00:00  (after 2026-11-25T06:00:00+08:00)
 1. 2026-11-25 09:00:00+08:00
 2. 2026-11-26 09:00:00+08:00
```

- 快捷写法：`minutely` `hourly` `daily` `midnight` `weekly` `monthly`
  `quarterly` `semi-annually` `yearly` `annually`。
- 可选星期前缀：`Mon`、`Mon..Fri`、`Sat,Sun`，支持英文全称。
- 日期 `[YYYY-]MM-DD`、时间 `HH:MM[:SS]`；每个组件支持 `*`、`N`、
  `N..M`、`N/M`、`N..M/M` 与逗号列表，月份可用英文名。
- 输出会同时打印归一化结果（如 `weekly` 展开为 `Mon *-*-* 00:00:00`），
  `--json` 中为 `normalized`。
- 星期与「日」是**与**关系，不同于经典 cron 的**或**语义。
- 8 年内不会触发时退出码 `3`，例如 `*-02-30 00:00`（2 月没有 30 号）。
- `~`（当月最后一天）、`?`、小数秒与末尾 `UTC` 时区后缀都会明确报错，
  时区请用 `--tz`。
- DST 处理与 cron 预览一致：不存在的挂钟时间跳过并提示，重复的时刻只列一次。

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
