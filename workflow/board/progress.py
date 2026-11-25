"""Progress and state display for evk-flash / evk-verify (stdlib only).

On a terminal: one live line (step, bar, bytes, rate, time left for the step and for the whole run) that is
redrawn in place; finished steps and board milestones are printed above it. When stdout is not a terminal (the
agent runs the tool in the background, or output goes to a file): no redraws, a line at each step and every 10 %
or 30 s. Either way the current state is written to status.json, which `evk-flash status` prints.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path


def cols(s: str) -> int:
    """Display width: CJK wide and full-width characters take two columns."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def wrap(s: str, width: int) -> list[str]:
    lines, cur = [], ""
    for ch in s:
        if cols(cur + ch) > width:
            lines.append(cur)
            cur = ""
        cur += ch
    return lines + ([cur] if cur else [])


def fmt_time(s: float | None) -> str:
    if s is None or s < 0:
        return "--"
    s = int(s + 0.5)
    return f"{s // 60}分{s % 60:02d}秒" if s >= 60 else f"{s}秒"


def fmt_bytes(n: float) -> str:
    for unit, k in (("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= k:
            return f"{n / k:.1f} {unit}" if n < 100 * k else f"{n / k:.0f} {unit}"
    return f"{int(n)} B"


class Progress:
    def __init__(self, status_path: Path | None = None, tty: bool | None = None, stream=None):
        self.out = stream or sys.stdout
        self.tty = self.out.isatty() if tty is None else tty
        self.color = self.tty and not os.environ.get("NO_COLOR")
        self.status_path = status_path
        self.t0 = time.time()
        self.plan: list[tuple[str, str, float]] = []        # (key, title, estimated seconds)
        self.i = 0
        self.key = self.title = self.detail = ""
        self.step_t0 = self.t0
        self.done = self.total = 0
        self.unit = "B"
        self.state = "running"
        self.human_msg = None
        self.wait_what = None
        self.wait_t0 = 0.0
        self.last_line = self.last_status = 0.0
        self.last_pct = -1
        self._live = False

    # ------------------------------------------------------------ plan and steps
    def set_plan(self, plan: list[tuple[str, str, float]]) -> None:
        self.plan = plan

    def start(self, key: str, detail: str = "") -> None:
        keys = [k for k, _, _ in self.plan]
        self.i = keys.index(key) + 1 if key in keys else self.i + 1
        self.key, self.detail = key, detail
        self.bar_t0 = None
        self.title = next((t for k, t, _ in self.plan if k == key), key)
        self.step_t0 = time.time()
        self.bar_t0 = None                  # the bar's own clock: a step can start with a wait for a person
        self.done = self.total = 0
        self.last_pct, self.last_line = -1, 0.0
        self.wait_what = None
        self._print(f"{self._c('1')}[{self.i}/{len(self.plan) or '?'}] {self.title}{self._c('0')}" +
                    (f"  {self._c('2')}{detail}{self._c('0')}" if detail else ""))
        self._status(force=True)

    def finish(self, summary: str = "", ok: bool = True) -> None:
        took = time.time() - self.step_t0
        mark = f"{self._c('32')}✓{self._c('0')}" if ok else f"{self._c('31;1')}✗{self._c('0')}"
        self._print(f"  {mark} {self.title}  {self._c('2')}({fmt_time(took)})" +
                    (f"  — {summary}" if summary else "") + self._c('0'))
        self.done = self.total = 0
        self.wait_what = None
        self._status(force=True)

    def milestone(self, text: str) -> None:
        self._print(f"  {self._c('36')}•{self._c('0')} {text}  {self._c('2')}(+{fmt_time(time.time() - self.step_t0)}){self._c('0')}")

    def note(self, text: str) -> None:
        self._print(f"  {text}")

    # ------------------------------------------------------------ bars and waits
    def update(self, done: float, total: float, unit: str = "B") -> None:
        if self.bar_t0 is None:
            self.bar_t0 = time.time()
        self.done, self.total, self.unit = done, total, unit
        self.wait_what = None
        now = time.time()
        pct = int(100 * done / total) if total else 0
        if self.tty:
            if now - self.last_line > 0.2 or done >= total:
                self._live_line(self._bar_text())
                self.last_line = now
        elif pct // 10 > self.last_pct // 10 or now - self.last_line > 30:
            self._print(f"  进度 {self._bar_text(bar=False)}")
            self.last_line = now
        self.last_pct = pct
        self._status(force=done >= total)

    def waiting(self, what: str) -> None:
        """Called repeatedly while waiting for the board or a person; shows the elapsed time."""
        now = time.time()
        if self.wait_what != what:
            self.wait_what, self.wait_t0 = what, now
            if not self.tty:
                self._print(f"  等待：{what}")
            self.last_line = now
        elapsed = now - self.wait_t0
        if self.tty and now - self.last_line > 0.25:
            spin = "|/-\\"[int(now * 4) % 4]
            self._live_line(f"  {spin} {what}  已等 {fmt_time(elapsed)}")
            self.last_line = now
        elif not self.tty and now - self.last_line > 60:
            self._print(f"  仍在等待：{what}（已等 {fmt_time(elapsed)}）")
            self.last_line = now
        self._status()

    def human(self, msg: str) -> None:
        self.state, self.human_msg = "waiting_for_human", msg
        width = min(shutil.get_terminal_size((100, 20)).columns - 2, 100)
        line = "─" * width
        bell = "\a" if self.tty else ""
        body = "\n".join("│ " + l for l in wrap(f">>> 需要你操作：{msg}", width - 2))
        self._print(f"{bell}{self._c('33;1')}┌{line}\n{body}\n└{line}{self._c('0')}")
        self._status(force=True)

    def human_done(self, what: str = "检测到板子有输出") -> None:
        if self.state == "waiting_for_human":
            self.state, self.human_msg = "running", None
            self._print(f"  {self._c('32')}✓{self._c('0')} {what}")
            self._status(force=True)

    def end(self, ok: bool, msg: str = "") -> None:
        self.state = "done" if ok else "failed"
        self.wait_what = self.human_msg = None
        self._print(("" if ok else self._c('31')) + f"{'完成' if ok else '失败'}：{msg}" + self._c('0') +
                    f"  （总计 {fmt_time(time.time() - self.t0)}）")
        self._status(force=True, extra={"message": msg})

    # ------------------------------------------------------------ rendering
    def eta_total(self) -> float | None:
        if not self.plan:
            return None
        keys = [k for k, _, _ in self.plan]
        rest = sum(e for k, _, e in self.plan[keys.index(self.key) + 1:]) if self.key in keys else 0
        cur = self._eta_step()
        if cur is None:
            est = next((e for k, _, e in self.plan if k == self.key), 0)
            cur = max(0.0, est - (time.time() - self.step_t0))
        return cur + rest

    def _eta_step(self) -> float | None:
        el = time.time() - (self.bar_t0 or self.step_t0)
        if not self.total or self.done <= 0 or el < 1:
            return None
        return (self.total - self.done) / (self.done / el)

    def _bar_text(self, bar: bool = True) -> str:
        el = max(time.time() - (self.bar_t0 or self.step_t0), 1e-3)
        pct = 100 * self.done / self.total if self.total else 0
        rate = self.done / el
        if self.unit == "B":
            amount, speed = f"{fmt_bytes(self.done)}/{fmt_bytes(self.total)}", f"{fmt_bytes(rate)}/s"
        else:
            amount, speed = f"{int(self.done)}/{int(self.total)} {self.unit}", ""
        b = ""
        if bar:
            w = 24
            fill = int(w * pct / 100)
            b = "█" * fill + "░" * (w - fill) + " "
        tot = self.eta_total()
        return (f"  {b}{pct:3.0f}%  {amount}  {speed}  剩余 {fmt_time(self._eta_step())}" +
                (f"  | 全部约剩 {fmt_time(tot)}" if tot is not None else ""))

    def _c(self, code: str) -> str:
        return f"\033[{code}m" if self.color else ""

    def _live_line(self, text: str) -> None:
        width = shutil.get_terminal_size((120, 20)).columns - 1
        while cols(text) > width:
            text = text[:-1]
        self.out.write("\r\033[K" + text)
        self.out.flush()
        self._live = True

    def _print(self, text: str) -> None:
        if self._live:
            self.out.write("\r\033[K")
            self._live = False
        self.out.write(text + "\n")
        self.out.flush()

    def _status(self, force: bool = False, extra: dict | None = None) -> None:
        if not self.status_path:
            return
        now = time.time()
        if not force and now - self.last_status < 0.5:
            return
        self.last_status = now
        st = {"state": self.state, "step": self.i, "steps": len(self.plan), "key": self.key, "title": self.title,
              "detail": self.detail, "human": self.human_msg, "waiting_for": self.wait_what,
              "done": self.done, "total": self.total, "unit": self.unit,
              "eta_step_s": self._eta_step(), "eta_total_s": self.eta_total(),
              "elapsed_s": round(now - self.t0, 1), "updated": time.strftime("%Y-%m-%dT%H:%M:%S"), **(extra or {})}
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        tmp.replace(self.status_path)


def describe(st: dict) -> str:
    """One status.json, for `evk-flash status`."""
    lines = [f"状态：{ {'running': '进行中', 'waiting_for_human': '等你操作', 'done': '已完成', 'failed': '失败'}.get(st['state'], st['state']) }"
             f"  （已运行 {fmt_time(st['elapsed_s'])}，更新于 {st['updated']}）",
             f"步骤：[{st['step']}/{st['steps']}] {st['title']}" + (f"  {st['detail']}" if st.get("detail") else "")]
    if st.get("human"):
        lines.append(f"需要你操作：{st['human']}")
    if st.get("waiting_for"):
        lines.append(f"正在等待：{st['waiting_for']}")
    if st.get("total"):
        unit = st["unit"]
        amt = (f"{fmt_bytes(st['done'])} / {fmt_bytes(st['total'])}" if unit == "B" else f"{int(st['done'])} / {int(st['total'])} {unit}")
        lines.append(f"进度：{100 * st['done'] / st['total']:.0f}%  {amt}  本步剩余 {fmt_time(st.get('eta_step_s'))}")
    if st.get("eta_total_s") is not None and st["state"] in ("running", "waiting_for_human"):
        lines.append(f"全部约剩：{fmt_time(st['eta_total_s'])}（不含等你操作的时间）")
    if st.get("message"):
        lines.append(f"结果：{st['message']}")
    return "\n".join(lines)
