"""پروفایل سبک مسیر پیام؛ فقط پیام‌های کند را لاگ می‌کند."""
import os
import time

_PERF_DEBUG = os.getenv("BOT_PERF_DEBUG", "").strip() == "1"


class MessagePerformance:
    STAGES = (
        "RECEIVE",
        "ADMIN_CHECK",
        "FILTER",
        "SPAM_CHECK",
        "FORWARD_CHECK",
        "COMMAND_MATCH",
        "AUTO_MODERATION",
        "SEND_RESPONSE",
    )

    def __init__(self):
        self.started_at = time.perf_counter()
        self._last_at = self.started_at
        self.values = {stage: 0.0 for stage in self.STAGES}
        # زیرمرحله‌های نام‌دار (مثلاً bio_check / banned_word / get_full_user)
        self.notes = {}

    def mark(self, stage):
        now = time.perf_counter()
        if stage in self.values:
            self.values[stage] += (now - self._last_at) * 1000
        self._last_at = now

    def skip_to(self):
        """Drop time since the last mark so the next stage is not a junk bucket."""
        self._last_at = time.perf_counter()

    def note(self, name, value_ms=None):
        """زمانِ یک زیرمرحلهٔ نام‌دار را ثبت می‌کند (بدون آرگومان = از آخرین mark).

        🎯 دلیل (خطِ لاگ ``PERF WARNING stage=RECEIVE elapsed_ms=1218.02``):
        قبلاً معلوم نبود داخلِ RECEIVE کدام کار ۱٫۲ ثانیه گرفته — پرشدنِ
        Governor با ``GetFullUserRequest``، بررسیِ بیو، یا precheck کلمات
        ممنوع. با ``note`` همان عدد در خطِ هشدار می‌آید:
        ``notes=bio_check=812.3,banned_word=0.6``.
        """
        if value_ms is None:
            now = time.perf_counter()
            value_ms = (now - self._last_at) * 1000
            self._last_at = now
        try:
            self.notes[name] = self.notes.get(name, 0.0) + max(0.0, float(value_ms))
        except (TypeError, ValueError):
            return self.notes.get(name, 0.0)
        return self.notes[name]

    def set(self, stage, value_ms):
        if stage in self.values:
            self.values[stage] = max(0.0, float(value_ms))

    def finish(self, logger, chat_id, threshold_ms=1000):
        now = time.perf_counter()
        total_ms = (now - self.started_at) * 1000
        slowest_stage = max(
            self.STAGES, key=lambda stage: self.values.get(stage, 0.0)
        )
        notes = {name: round(value, 2) for name, value in self.notes.items()}
        result = {
            "total_ms": total_ms,
            "slowest_stage": slowest_stage,
            "slowest_ms": self.values.get(slowest_stage, 0.0),
            "stages": dict(self.values),
            "notes": notes,
        }
        if total_ms < threshold_ms:
            return result
        # خلاصهٔ یک‌خطی: کدام مرحله و کدام زیرمرحله وقت را خورده است.
        breakdown = ",".join(
            f"{stage.lower()}={self.values[stage]:.1f}"
            for stage in sorted(self.STAGES, key=lambda s: -self.values.get(s, 0.0))[:3]
            if self.values.get(stage, 0.0) > 0
        ) or "-"
        notes_text = ",".join(
            f"{name}={value:.1f}" for name, value in
            sorted(notes.items(), key=lambda item: -item[1])[:4]
        ) or "-"
        try:
            logger.log_error(
                f"PERF SLOW chat_id={chat_id} total_ms={total_ms:.2f} "
                f"slowest={slowest_stage.lower()}:{self.values.get(slowest_stage, 0.0):.1f}ms "
                f"breakdown={breakdown} notes={notes_text}"
            )
        except Exception:
            pass
        parts = []
        for stage in self.STAGES:
            value = self.values[stage]
            parts.append(f"{stage.lower()}={value:.2f}ms")
            if value > threshold_ms:
                logger.log_error(
                    f"PERF WARNING chat_id={chat_id} stage={stage} "
                    f"elapsed_ms={value:.2f} "
                    f"slowest={slowest_stage.lower()}:{self.values.get(slowest_stage, 0.0):.1f}ms "
                    f"notes={notes_text}"
                )
        if _PERF_DEBUG:
            logger.log_info(
                "PERF " + " ".join(parts) + f" TOTAL={total_ms:.2f}ms"
            )
        return result
