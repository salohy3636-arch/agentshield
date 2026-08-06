# AgentShield AI — دليل التشغيل والنشر

## 1. كيف ترتبط الملفات ببعضها

```
agentshield/
├── app/
│   ├── main.py                 ← FastAPI: كل الـ endpoints + يقدّم مجلد web/ كموقع ثابت
│   ├── database.py             ← اتصال SQLAlchemy + نماذج الجداول (SQLite محلياً / Postgres للإنتاج)
│   ├── risk_engine.py          ← محرك درجة المخاطرة (يُستدعى من main.py)
│   ├── crypto_logger.py        ← سجل التدقيق المشفّر (مخزَّن عبر database.py)
│   └── monetization_claims.py  ← Stripe + الأرصدة + التعويض (مخزَّنة عبر database.py)
├── web/
│   ├── index.html              ← صفحة التسويق والأسعار
│   ├── dashboard.html          ← لوحة التحكم الحية
│   └── assets/
│       ├── styles.css / dashboard.css
│       ├── app.js              ← تشغيل صفحة الأسعار + الاتصال بـ /v1/billing/checkout-session
│       └── dashboard.js        ← يقرأ بيانات حية من /v1/stats و /v1/ledger/recent و... إلخ
├── LEGAL_TERMS.md
├── requirements.txt
├── Dockerfile
├── docker-compose.yml          ← يشغّل API + Postgres معاً محلياً بأمر واحد
└── .env.example
```

**كيف يعمل الربط فعلياً:** عند تشغيل `main.py`، آخر سطر فيه يُركّب (mount) مجلد `web/`
ليُقدَّم كموقع ثابت من نفس الخدمة، على نفس النطاق والمنفذ. هذا يعني:

- زيارة `/` تعرض `index.html` (صفحة التسويق).
- زيارة `/dashboard.html` تعرض لوحة التحكم.
- كل طلبات JavaScript من الواجهة (`fetch(...)`) تذهب إلى نفس الخادم على مسارات
  مثل `/v1/stats`, `/v1/ledger/recent`, `/v1/billing/checkout-session` — **بدون
  الحاجة لإعداد CORS أو نطاق منفصل**، لأنها كلها خدمة واحدة.

إذا أردت لاحقاً فصل الواجهة عن الـ API (مثلاً الواجهة على Vercel والـ API على Render)،
غيّر فقط `window.AGENTSHIELD_API_BASE` في `web/assets/app.js` و `dashboard.html`
ليشير لعنوان الـ API، وفعّل `AGENTSHIELD_ALLOWED_ORIGINS` في `.env`.

## 2. التشغيل محلياً

```bash
cd agentshield
python -m venv .venv && source .venv/bin/activate   # أو .venv\Scripts\activate على ويندوز
pip install -r requirements.txt
cp .env.example .env   # ثم عدّل القيم
cp LEGAL_TERMS.md web/LEGAL_TERMS.md   # حتى يظهر الرابط في الموقع

cd app
export $(cat ../.env | xargs)   # على ماك/لينكس، أو استخدم python-dotenv
uvicorn main:app --reload --port 8000
```

افتح `http://localhost:8000` للموقع، و`http://localhost:8000/dashboard.html` للوحة.
اضغط زر **"تعبئة بيانات تجريبية"** في اللوحة لرؤية بيانات حية فوراً دون ربط وكيل حقيقي.

## 3. ربط وكلائك الفعليين

كل وكيل ذكاء اصطناعي (بوت دعم، بوت فوترة، ...) يستدعي قبل تنفيذ أي إجراء:

```bash
curl -X POST http://localhost:8000/v1/guard \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "billing-bot-01",
    "action_type": "issue_refund",
    "financial_value": 250,
    "payload": {"order_id": "A123"}
  }'
```

الاستجابة تحدد `decision`: `pass` (نفّذ الإجراء فعلياً في نظامك)، `hold` (انتظر
قرار إنسان عبر `/v1/approvals/{token}/decide`)، أو `block` (لا تنفّذ الإجراء).

## 4. إعداد الاشتراكات الشهرية/السنوية (Stripe)

1. في [Stripe Dashboard](https://dashboard.stripe.com) أنشئ 3 منتجات: Starter, Pro,
   Enterprise — ولكل منتج **سعرين**: شهري وسنوي (بالقيم الظاهرة في `index.html`).
2. انسخ كل Price ID إلى المتغيرات المقابلة في `.env`
   (`STRIPE_PRICE_STARTER_MONTHLY`... إلخ).
3. أضف `STRIPE_SECRET_KEY` من إعدادات API في Stripe.
4. أنشئ Webhook Endpoint في Stripe يشير إلى
   `https://your-domain.com/v1/webhooks/stripe` لحدث `invoice.payment_succeeded`
   على الأقل، وانسخ الـ Signing Secret إلى `STRIPE_WEBHOOK_SECRET`.
5. جرّب الدفع الآن: من صفحة الأسعار، اضغط أي زر اشتراك، سيفتح Stripe Checkout
   الحقيقي (استخدم [بطاقات الاختبار](https://stripe.com/docs/testing) في وضع Test).

بدون هذا الإعداد، سيظهر للزائر خطأ واضح ("لم يتم إعداد سعر Stripe لهذه الخطة")
بدل فشل صامت — هذا مقصود حتى تكتشف أي خطة ناقصة الإعداد قبل الإطلاق.

## 5. النشر للإنتاج

**الخيار الأبسط — خدمة واحدة (Render / Fly.io / Railway):**
```bash
docker build -t agentshield .
docker run -p 8000:8000 --env-file .env agentshield
```
اربط أي من هذه المنصات بمستودع Git الخاص بك، وسيبني الـ `Dockerfile` تلقائياً
ويشغّل الموقع + الـ API معاً على نطاق واحد.

**قبل الإطلاق الفعلي، تأكد من:**
- [ ] `AGENTSHIELD_DEMO_MODE=false` (لإيقاف نقطة تعبئة البيانات التجريبية للعامة)
- [ ] `AGENTSHIELD_ALLOWED_ORIGINS` محدد بنطاقك الحقيقي وليس `*`
- [ ] `DATABASE_URL` يشير إلى قاعدة Postgres حقيقية وليس SQLite المحلي (انظر القسم 7)
- [ ] مراجعة `LEGAL_TERMS.md` من محامٍ مرخّص في نطاق ولايتك القضائية قبل نشره
      كشروط خدمة فعلية، خاصة قسم التعويض النقدي
- [ ] تفعيل HTTPS (تلقائي على معظم منصات الاستضافة المُدارة)

## 7. قاعدة البيانات (تم استبدال التخزين المؤقت بالكامل)

كل الحالة الآن دائمة عبر SQLAlchemy في `app/database.py`، ولم يعد هناك أي
قاموس Python في الذاكرة:

| البيانات | كانت | صارت |
|---|---|---|
| سجل التدقيق (ledger) | sqlite3 يدوي منفصل | جدول `ledger_entries` عبر SQLAlchemy |
| الحسابات والاشتراكات | `dict` في الذاكرة | جدول `accounts` |
| رصيد الاحتياطي/الأرباح | متغيرات في الذاكرة | جدول `reserve_state` (صف واحد) |
| طلبات المراجعة المعلّقة | `dict` في الذاكرة | جدول `pending_approvals` |

**محلياً (بدون أي إعداد):** `DATABASE_URL` غير محدد → يُستخدم ملف
`agentshield.db` (SQLite حقيقي على القرص، يبقى بعد إعادة التشغيل، لكن غير
مناسب لأكثر من عملية خادم واحدة في نفس الوقت).

**للإنتاج (Postgres):**

الخيار الأول — تشغيل محلي بـ Docker Compose (يشغّل Postgres + الـ API معاً):
```bash
docker compose up --build
```

الخيار الثاني — Postgres مُدار (Render / Railway / Neon / Supabase / RDS):
1. أنشئ قاعدة بيانات Postgres من لوحة تحكم المزوّد.
2. انسخ رابط الاتصال (Connection String) وضعه في `DATABASE_URL` بصيغة:
   `postgresql+psycopg2://user:password@host:5432/dbname`
3. أعد تشغيل الخدمة — الجداول تُنشأ تلقائياً عند الإقلاع (`init_db()` في
   `database.py`)، لا حاجة لأي أمر migration يدوي عند الإطلاق الأول.

**ملاحظة عن Redis:** غير ضروري لسلامة البيانات — Postgres وحده يكفي ويضمن
عمل النظام بشكل صحيح حتى لو شغّلت أكثر من نسخة من الخادم. أضِفه لاحقاً فقط لو
احتجت تحديثات فورية عبر WebSocket/pub-sub أو تخزين مؤقت لحجم ضخم من الطلبات —
ليس مطلوباً للإطلاق.

## 8. فحص سريع أن كل شيء يعمل

```bash
curl http://localhost:8000/healthz            # {"status":"ok"}
curl http://localhost:8000/v1/ledger/verify    # {"valid": true, ...}
curl -X POST http://localhost:8000/v1/demo/seed
curl http://localhost:8000/v1/stats
```

بعد إعادة تشغيل الخادم (`docker compose restart api` أو إعادة نشر)، شغّل
`curl .../v1/stats` مرة أخرى — يجب أن ترى نفس الأرقام السابقة (وليس صفراً)،
وهذا يؤكد أن البيانات فعلاً دائمة الآن.

