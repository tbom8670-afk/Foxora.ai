#!/usr/bin/env bash
# 🔧 رفع خطای «git pull» به‌خاطر فایل‌های __pycache__
#
#   bash tools/fix_pull.sh
#
# فایل‌های .pyc کامپایل‌شده هستند و ارزش نگهداری ندارند؛ پایتون دوباره
# می‌سازدشان. این اسکریپت آن‌ها را کنار می‌زند تا pull بدون خطا انجام شود.
# هیچ فایل سورس یا فایل runtime (config/*.json و logs/) را دست نمی‌زند.

set -u
cd "$(dirname "$0")/.." || exit 1

echo "🔧 رفع مشکل pull"
echo "پوشه: $(pwd)"
echo

# ۱) اگر نسخهٔ محلیِ .pyc تغییر کرده، همان‌ها جلوی merge را می‌گیرند.
#    برشان می‌گردانیم تا git آزاد باشد.
tracked_pyc=$(git ls-files | grep -E '\.pyc$|\.pyo$' || true)
if [ -n "$tracked_pyc" ]; then
    echo "↩️  برگرداندن نسخهٔ .pyc های ردیابی‌شده..."
    echo "$tracked_pyc" | xargs -r git checkout -- 2>/dev/null
    echo "   انجام شد."
else
    echo "✅ هیچ .pyc ردیابی‌شده‌ای نیست."
fi

# ۲) اگر هنوز چیزی از bytecode در stage مانده، از index خارجش می‌کنیم.
#    فایل محلی پاک نمی‌شود، فقط git دیگر ردیابی‌اش نمی‌کند.
if [ -n "$tracked_pyc" ]; then
    echo "🧹 خارج کردن bytecode از ردیابی git (فایل محلی حفظ می‌شود)..."
    echo "$tracked_pyc" | xargs -r git rm -r --cached --quiet 2>/dev/null
fi

# ۳) پاک کردن bytecode یتیم و کهنه از دیسک. پایتون بازسازی می‌کند.
echo "🗑️  پاک کردن __pycache__ از دیسک..."
find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null
find . -name "*.pyo" -delete 2>/dev/null
echo "   انجام شد."

# ۴) حالا pull باید تمیز باشد.
echo
echo "⬇️  اجرای git pull..."
if git pull --no-rebase origin main; then
    echo
    echo "✅ pull با موفقیت انجام شد."
else
    echo
    echo "⚠️  pull هنوز خطا داد. اگر فقط فایل‌های runtime مانع‌اند:"
    echo "     git stash push -- config/ logs/"
    echo "     git pull origin main"
    echo "     git stash pop"
    echo
    echo "   یا برای بازنشانی کامل به نسخهٔ سرور (تغییرات محلی از بین می‌رود):"
    echo "     git fetch origin && git reset --hard origin/main"
    exit 1
fi

# ۵) گزارش وضعیت سورس.
echo
echo "📋 بررسی سلامت:"
for f in handlers/economy_handler.py economy/__init__.py \
         economy/ui/balance_menu.py economy/ui/shop_menu.py; do
    if [ -f "$f" ]; then
        echo "   ✅ $f"
    else
        echo "   ❌ $f  ← غایب!"
    fi
done

echo
echo "▶️  گام بعدی: ربات را دوباره اجرا کنید"
echo "     pkill -f 'python3 main.py'"
echo "     python3 main.py"
