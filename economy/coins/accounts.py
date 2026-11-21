"""🪙 حساب‌های سکه — هستهٔ عملیات اتمیک.

هر عملیات داخل ``storage.transaction()`` انجام می‌شود، پس:
  • دو عملیات هم‌زمان موجودی را خراب نمی‌کنند.
  • موجودی هرگز منفی نمی‌شود (پیش از کسر بررسی می‌گردد).
  • ``total_coin_value`` بعد از هر تغییر دوباره محاسبه و ذخیره می‌شود.
  • ثبت تاریخچه در همان تراکنش انجام می‌گیرد.
"""
from datetime import datetime, timezone
import importlib
import logging

from economy import settings, storage
from economy.transactions import ledger

BRONZE = settings.BRONZE
SILVER = settings.SILVER
GOLD = settings.GOLD
COIN_TYPES = settings.COIN_TYPES

OWNER_SILVER = 200000
OWNER_SILVER_GROUPS = frozenset({"22770700", "9429374"})


def is_main_owner(user_id):
    """Economy owner checks read the canonical get_owner() source lazily.

    The lazy import preserves the economy package's no-bot-import boundary at
    module load time while avoiding any embedded owner ID.
    """
    get_owner = importlib.import_module("modules.owner_check").get_owner
    owner_id = get_owner().get("user_id")
    if owner_id is None:
        return False
    try:
        return int(user_id) == int(owner_id)
    except (TypeError, ValueError):
        return False


class EconomyError(Exception):
    """خطای قابل‌انتظار اقتصاد (موجودی کم، ورودی نامعتبر و…)."""


_CHANNEL_ID_OFFSET = 1_000_000_000_000


def chat_key(chat_id):
    """کلید پایدار گروه: ‎-100123 و 123 به یک کلید نگاشت می‌شوند."""
    try:
        value = int(chat_id)
    except (TypeError, ValueError):
        return str(chat_id)
    if value <= -_CHANNEL_ID_OFFSET:
        value = abs(value) - _CHANNEL_ID_OFFSET
    elif value <= -1000000000 and str(value).startswith("-100"):
        value = abs(value) - 10000000000
    elif value < 0:
        value = abs(value)
    return str(value)


def normalize_user_id(user_id):
    """شناسه کاربر را برای کلید کیف‌پول یکسان می‌کند."""
    try:
        return str(int(user_id))
    except (TypeError, ValueError):
        return str(user_id)


def chat_aliases(chat_id):
    """املاهای تاریخی همان گروه که ممکن است در دیتابیس مانده باشد."""
    aliases = []

    def _add(value):
        text = str(value)
        if text and text not in aliases:
            aliases.append(text)

    _add(chat_key(chat_id))
    _add(chat_id)
    try:
        value = int(chat_id)
    except (TypeError, ValueError):
        return aliases
    _add(value)
    _add(abs(value))
    _add(-abs(value))
    digits = str(abs(value))
    if digits.startswith("100") and len(digits) > 6:
        stripped = digits[3:].lstrip("0") or "0"
        _add(digits[3:])
        _add(stripped)
        _add("-" + stripped)
        _add("-100" + stripped)
    else:
        _add("100" + digits)
        _add("-100" + digits)
        _add(-(10_000_000_000 + abs(value)))
    return aliases


def candidate_user_keys(chat_id, user_id):
    """کلیدهای محتمل کیف‌پول برای همین کاربر در همین گروه."""
    user_ids = []
    for raw in (user_id, normalize_user_id(user_id), str(user_id)):
        text = str(raw)
        if text and text not in user_ids:
            user_ids.append(text)
    keys = []
    for group in chat_aliases(chat_id):
        for uid in user_ids:
            key = f"{group}:{uid}"
            if key not in keys:
                keys.append(key)
    return keys


def resolve_user_key(chat_id, user_id):
    """کیف‌پول موجود را پیدا می‌کند؛ وگرنه کلید متعارف را برمی‌گرداند."""
    canonical = f"{chat_key(chat_id)}:{normalize_user_id(user_id)}"
    for key in candidate_user_keys(chat_id, user_id):
        if storage.user_fields(key, (BRONZE,)) is not None:
            return key
    return canonical


def user_key(chat_id, user_id):
    """کلید کیف پول: هر کاربر در هر گروه حساب جداگانه دارد."""
    return f"{chat_key(chat_id)}:{normalize_user_id(user_id)}"


def split_key(key):
    """``"123:456"`` → ``("123", "456")``."""
    chat, _, user = str(key).partition(":")
    return chat, user


def _now():
    return datetime.now(timezone.utc).isoformat()


def _blank_user():
    return {
        BRONZE: 0,
        SILVER: 0,
        GOLD: 0,
        "total_coin_value": 0,
        "transactions": [],
        "references": [],
        "wins": 0,
        "name": None,
        "created_at": _now(),
        # مهر ترتیبی لحظه‌ای که کاربر به ارزش فعلی رسیده است. برای
        # تساوی در رتبه‌بندی استفاده می‌شود: هرکس زودتر رسیده، بالاتر.
        "value_reached_seq": 0,
        "value_reached_at": None,
    }


def _user(data, key):
    users = data.setdefault("users", {})
    user = users.get(key)
    if user is None:
        user = _blank_user()
        users[key] = user
    for coin in COIN_TYPES:
        user.setdefault(coin, 0)
    user.setdefault("total_coin_value", 0)
    user.setdefault("transactions", [])
    user.setdefault("references", [])
    user.setdefault("value_reached_seq", 0)
    user.setdefault("wins", 0)
    return user


def _validate_amount(amount):
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise EconomyError("مقدار سکه باید عدد صحیح باشد.")
    if amount <= 0:
        raise EconomyError("مقدار سکه باید بزرگ‌تر از صفر باشد.")
    return amount


def _validate_coin(coin_type):
    if coin_type not in COIN_TYPES:
        raise EconomyError(f"نوع سکه نامعتبر است: {coin_type!r}")
    return coin_type


def compute_total_value(user):
    """ارزش کل از روی موجودی و ارزش‌های تنظیمات."""
    values = settings.coin_values()
    return sum(int(user.get(coin, 0)) * int(values[coin])
               for coin in COIN_TYPES)


def _refresh_total(data, user):
    """``total_coin_value`` را دوباره می‌سازد و زمان رسیدن را ثبت می‌کند."""
    previous = int(user.get("total_coin_value", 0))
    current = compute_total_value(user)
    user["total_coin_value"] = current
    if current != previous:
        # فقط وقتی ارزش عوض شود مهر زمانی تازه می‌شود، پس کسی که زودتر به
        # این مقدار رسیده، مهر کوچک‌تر و در نتیجه رتبهٔ بالاتری دارد.
        user["value_reached_seq"] = storage.next_sequence(data)
        user["value_reached_at"] = _now()
    return current


def is_owner_silver_group(chat_id, user_id):
    return is_main_owner(user_id) and chat_key(chat_id) in OWNER_SILVER_GROUPS


def _owner_balance(chat_id, user_id):
    user = storage.user_fields(resolve_user_key(chat_id, user_id), COIN_TYPES) or {}
    balance = {coin: int(user.get(coin, 0) or 0) for coin in COIN_TYPES}
    balance[SILVER] = OWNER_SILVER
    balance["total_coin_value"] = compute_total_value({**user, SILVER: OWNER_SILVER})
    return balance


def _snapshot_balance(user):
    """موجودی فعلی به‌همراه ارزش کل.

    ارزش کل *همیشه* از روی موجودی واقعی سکه‌ها محاسبه می‌شود، نه از روی
    فیلد ذخیره‌شده. آن فیلد فقط یک کش برای رتبه‌بندی است و می‌تواند از
    واقعیت عقب بماند (مثلاً در مسیر «مرجع تکراری» که بدون بازمحاسبه
    برمی‌گردد، یا وقتی ارزش سکه‌ها در تنظیمات عوض شود).
    """
    balance = {coin: int(user.get(coin, 0)) for coin in COIN_TYPES}
    balance["total_coin_value"] = compute_total_value(user)
    return balance


# ---------------------------------------------------------------------------
# افزودن و کسر
# ---------------------------------------------------------------------------
def add(chat_id, user_id, coin_type, amount, *, kind=ledger.KIND_RECEIVE,
        reference=None, note=None, name=None, win=False):
    """افزودن سکه. خروجی: موجودی جدید.

    اگر ``reference`` تکراری باشد هیچ تغییری اعمال نمی‌شود و موجودی فعلی
    برگردانده می‌شود — این همان محافظ «جلوگیری از ثبت دوبارهٔ تراکنش» است.

    ``win=True`` شمارندهٔ بردها را یکی زیاد می‌کند (برای پروفایل بازیکن).
    """
    _validate_coin(coin_type)
    _validate_amount(amount)
    if is_owner_silver_group(chat_id, user_id) and coin_type == SILVER:
        return _owner_balance(chat_id, user_id)
    key = resolve_user_key(chat_id, user_id)

    with storage.transaction() as data:
        if ledger.is_duplicate(data, key, reference):
            return _snapshot_balance(_user(data, key))
        user = _user(data, key)
        user[coin_type] = int(user.get(coin_type, 0)) + int(amount)
        if name:
            user["name"] = str(name)
        if win:
            user["wins"] = int(user.get("wins", 0)) + 1
            # 🏅 برد per-game برای «استاد بازی» در کارت دستاورد؛ فقط
            # همین‌جا که جایزه واقعاً پرداخت شده (مرجع تکراری بالاتر
            # برگشته) و فقط با نام بازیِ داخل note — بدون حدس.
            try:
                from modules import user_achievements
                user_achievements.record_game_win(chat_id, user_id, note)
            except Exception:
                pass
        total = _refresh_total(data, user)
        ledger.record(
            data, key, kind, {coin_type: amount},
            reference=reference, note=note,
            balance_after=_snapshot_balance(user), total_value=total,
        )
        return _snapshot_balance(user)


def remove(chat_id, user_id, coin_type, amount, *, kind=ledger.KIND_SPEND,
           reference=None, note=None):
    """کسر سکه. اگر موجودی کافی نباشد ``EconomyError`` می‌دهد."""
    _validate_coin(coin_type)
    _validate_amount(amount)
    if is_owner_silver_group(chat_id, user_id) and coin_type == SILVER:
        return _owner_balance(chat_id, user_id)
    key = resolve_user_key(chat_id, user_id)

    with storage.transaction() as data:
        if ledger.is_duplicate(data, key, reference):
            return _snapshot_balance(_user(data, key))
        user = _user(data, key)
        current = int(user.get(coin_type, 0))
        if current < amount:
            raise EconomyError(
                f"موجودی {settings.COIN_LABELS[coin_type]} کافی نیست: "
                f"{current} < {amount}"
            )
        user[coin_type] = current - int(amount)
        total = _refresh_total(data, user)
        ledger.record(
            data, key, kind, {coin_type: -amount},
            reference=reference, note=note,
            balance_after=_snapshot_balance(user), total_value=total,
        )
        return _snapshot_balance(user)


# --- میان‌برهای نوع‌دار ------------------------------------------------------
def add_bronze(chat_id, user_id, amount, **kwargs):
    return add(chat_id, user_id, BRONZE, amount, **kwargs)


def add_silver(chat_id, user_id, amount, **kwargs):
    return add(chat_id, user_id, SILVER, amount, **kwargs)


def add_gold(chat_id, user_id, amount, **kwargs):
    return add(chat_id, user_id, GOLD, amount, **kwargs)


def remove_bronze(chat_id, user_id, amount, **kwargs):
    return remove(chat_id, user_id, BRONZE, amount, **kwargs)


def remove_silver(chat_id, user_id, amount, **kwargs):
    return remove(chat_id, user_id, SILVER, amount, **kwargs)


def remove_gold(chat_id, user_id, amount, **kwargs):
    return remove(chat_id, user_id, GOLD, amount, **kwargs)


# ---------------------------------------------------------------------------
# تبدیل
# ---------------------------------------------------------------------------
def _convert(chat_id, user_id, source, target, cost, gain, times, reference, note):
    if isinstance(times, bool) or not isinstance(times, int) or times <= 0:
        raise EconomyError("تعداد تبدیل باید عدد صحیح مثبت باشد.")
    total_cost = cost * times
    total_gain = gain * times
    key = resolve_user_key(chat_id, user_id)

    with storage.transaction() as data:
        if ledger.is_duplicate(data, key, reference):
            return _snapshot_balance(_user(data, key))
        user = _user(data, key)
        special_owner = is_owner_silver_group(chat_id, user_id) and source == SILVER
        current = OWNER_SILVER if special_owner else int(user.get(source, 0))
        if current < total_cost:
            raise EconomyError(
                f"موجودی {settings.COIN_LABELS[source]} کافی نیست: "
                f"{current} < {total_cost}"
            )
        if not special_owner:
            user[source] = current - total_cost
        user[target] = int(user.get(target, 0)) + total_gain
        total = _refresh_total(data, user)
        ledger.record(
            data, key, ledger.KIND_CONVERT,
            {source: -total_cost, target: total_gain},
            reference=reference, note=note,
            balance_after=_snapshot_balance(user), total_value=total,
        )
        return _snapshot_balance(user)


def convert_bronze(chat_id, user_id, times=1, *, reference=None, note=None):
    """۱۰۰ برنز ➜ ۱۰ نقره (به ازای هر بار)."""
    config = settings.load()
    return _convert(
        chat_id, user_id, BRONZE, SILVER,
        int(config["BronzeToSilverCost"]), int(config["BronzeToSilverGain"]),
        times, reference, note,
    )


def convert_silver(chat_id, user_id, times=1, *, reference=None, note=None):
    """۷۰ نقره ➜ ۱۰ طلا (به ازای هر بار)."""
    config = settings.load()
    return _convert(
        chat_id, user_id, SILVER, GOLD,
        int(config["SilverToGoldCost"]), int(config["SilverToGoldGain"]),
        times, reference, note,
    )


# ---------------------------------------------------------------------------
# انتقال
# ---------------------------------------------------------------------------
def transfer(chat_id, sender_id, receiver_id, coin_type, amount, *,
             reference=None, note=None):
    """انتقال سکه بین دو کاربر — کاملاً اتمیک.

    یا هر دو طرف تغییر می‌کنند یا هیچ‌کدام.
    """
    _validate_coin(coin_type)
    _validate_amount(amount)
    sender = resolve_user_key(chat_id, sender_id)
    receiver = resolve_user_key(chat_id, receiver_id)
    if sender == receiver:
        raise EconomyError("انتقال به خودتان ممکن نیست.")

    with storage.transaction() as data:
        if ledger.is_duplicate(data, sender, reference):
            return {
                "sender": _snapshot_balance(_user(data, sender)),
                "receiver": _snapshot_balance(_user(data, receiver)),
            }
        from_user = _user(data, sender)
        to_user = _user(data, receiver)
        special_owner = is_owner_silver_group(chat_id, sender_id) and coin_type == SILVER
        current = OWNER_SILVER if special_owner else int(from_user.get(coin_type, 0))
        if current < amount:
            raise EconomyError(
                f"موجودی {settings.COIN_LABELS[coin_type]} کافی نیست: "
                f"{current} < {amount}"
            )

        if not special_owner:
            from_user[coin_type] = current - int(amount)
        to_user[coin_type] = int(to_user.get(coin_type, 0)) + int(amount)
        from_total = _refresh_total(data, from_user)
        to_total = _refresh_total(data, to_user)

        ledger.record(
            data, sender, ledger.KIND_TRANSFER_OUT, {coin_type: -amount},
            reference=reference, note=note, counterparty=receiver,
            balance_after=_snapshot_balance(from_user), total_value=from_total,
        )
        ledger.record(
            data, receiver, ledger.KIND_TRANSFER_IN, {coin_type: amount},
            reference=f"{reference}:in" if reference else None,
            note=note, counterparty=sender,
            balance_after=_snapshot_balance(to_user), total_value=to_total,
        )
        return {
            "sender": (_owner_balance(chat_id, sender_id)
                       if special_owner else _snapshot_balance(from_user)),
            "receiver": _snapshot_balance(to_user),
        }


# ---------------------------------------------------------------------------
# خواندن
# ---------------------------------------------------------------------------
def get_balance(chat_id, user_id):
    """موجودی هر سه سکه به‌همراه ارزش کل."""
    if is_owner_silver_group(chat_id, user_id):
        logging.getLogger("economy").info(
            "OWNER BONUS BALANCE DEBUG owner=True group_match=True silver=200000 "
            f"user_id={user_id} chat_id={chat_id}"
        )
        return _owner_balance(chat_id, user_id)
    user = storage.user_fields(
        resolve_user_key(chat_id, user_id), (*COIN_TYPES, "total_coin_value")
    )
    if not user:
        return {BRONZE: 0, SILVER: 0, GOLD: 0, "total_coin_value": 0}
    balance = {coin: int(user.get(coin, 0)) for coin in COIN_TYPES}
    # ارزش همیشه از روی تنظیمات فعلی محاسبه می‌شود تا تغییر تنظیمات
    # بلافاصله بازتاب پیدا کند.
    balance["total_coin_value"] = compute_total_value(user)
    return balance


def calculate_total_value(chat_id, user_id):
    """ارزش کل کاربر؛ همیشه از موجودی فعلی بازمحاسبه می‌شود."""
    return get_balance(chat_id, user_id)["total_coin_value"]


def recalculate(chat_id, user_id):
    """ارزش کل را بازمحاسبه و ذخیره می‌کند (پس از تغییر تنظیمات)."""
    key = resolve_user_key(chat_id, user_id)
    with storage.transaction() as data:
        user = _user(data, key)
        return _refresh_total(data, user)


def recalculate_all():
    """ارزش همهٔ کاربران را بازمحاسبه می‌کند."""
    with storage.transaction() as data:
        for key in list(data.get("users", {})):
            _refresh_total(data, _user(data, key))
        return len(data.get("users", {}))


def set_name(chat_id, user_id, name):
    """نام نمایشی کاربر را نگه می‌دارد (برای جدول رتبه‌بندی)."""
    if not name:
        return None
    key = resolve_user_key(chat_id, user_id)
    name = str(name)
    # Opening «موجودی» calls this on every request.  Avoid a full atomic JSON
    # rewrite/fsync when the display name has not changed.
    current = storage.user_fields(key, ("name",))
    if current is not None and current.get("name") == name:
        return name
    with storage.transaction() as data:
        user = _user(data, key)
        user["name"] = name
        return user["name"]


def get_profile(chat_id, user_id):
    """پروفایل کامل: موجودی، ارزش کل، بردها و نام."""
    key = resolve_user_key(chat_id, user_id)
    user = storage.user_fields(
        key, (*COIN_TYPES, "total_coin_value", "wins", "name")
    )
    if is_main_owner(user_id):
        user = user or {}
        profile = _owner_balance(chat_id, user_id)
        profile["wins"] = int(user.get("wins", 0) or 0)
        profile["name"] = user.get("name")
        return profile
    if not user:
        return {BRONZE: 0, SILVER: 0, GOLD: 0, "total_coin_value": 0,
                "wins": 0, "name": None}
    profile = {coin: int(user.get(coin, 0) or 0) for coin in COIN_TYPES}
    profile["total_coin_value"] = compute_total_value(user)
    profile["wins"] = int(user.get("wins", 0))
    profile["name"] = user.get("name")
    return profile


def all_users():
    """کپی فقط-خواندنی از همهٔ حساب‌ها."""
    return storage.snapshot().get("users", {})
