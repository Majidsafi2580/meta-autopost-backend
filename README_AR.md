# Meta AutoPost Backend

هذا Backend أولي لربط بوتك مع Meta:
- Privacy Policy
- Terms
- Data Deletion
- OAuth start/callback
- جلب Facebook Pages
- جلب Instagram business account المرتبط بالصفحة
- تجربة نشر Facebook text post
- تجربة نشر Instagram photo post

## الملفات
```text
server.py
requirements.txt
Procfile
.env.example
```

## التشغيل المحلي في Termux أو PC

```bash
pip install -r requirements.txt
cp .env.example .env
python server.py
```

## الرفع على Render

1. ارفع الملفات إلى GitHub repo.
2. افتح Render.
3. New Web Service.
4. اربط GitHub repo.
5. Build Command:
```bash
pip install -r requirements.txt
```
6. Start Command:
```bash
gunicorn server:app
```

## Environment Variables في Render

حط:

```text
META_APP_ID=...
META_APP_SECRET=...
BASE_URL=https://your-app-name.onrender.com
META_REDIRECT_URI=https://your-app-name.onrender.com/auth/meta/callback
META_GRAPH_VERSION=v25.0
```

اختياري:
```text
TELEGRAM_BOT_TOKEN=...
ADMIN_CHAT_ID=...
```

## روابط تضعها في Meta App Basic Settings

Privacy Policy URL:
```text
https://your-app-name.onrender.com/privacy
```

Terms URL:
```text
https://your-app-name.onrender.com/terms
```

Data Deletion URL:
```text
https://your-app-name.onrender.com/data-deletion
```

Valid OAuth Redirect URI:
```text
https://your-app-name.onrender.com/auth/meta/callback
```

## تجربة الربط

افتح:
```text
https://your-app-name.onrender.com/auth/meta/start?telegram_id=admin
```

بعد الموافقة من Facebook، ارجع:
```text
https://your-app-name.onrender.com/accounts
```

## تجربة نشر Facebook

استعمل curl:

```bash
curl -X POST https://your-app-name.onrender.com/post/facebook \
  -H "Content-Type: application/json" \
  -d '{"message":"Test from AutoPost DZ"}'
```

## تجربة نشر Instagram صورة

الصورة لازم تكون URL مباشر عام، مثلا من Cloudinary:

```bash
curl -X POST https://your-app-name.onrender.com/post/instagram/photo \
  -H "Content-Type: application/json" \
  -d '{"image_url":"https://example.com/image.jpg","caption":"Test Instagram post"}'
```

## ملاحظات مهمة

- لا تطلب من المستخدم Facebook/Instagram password.
- لا ترسل App Secret في الشات.
- التخزين في accounts.json مناسب للتجربة فقط. في النسخة الجدية استعمل PostgreSQL.
- قبل البيع للناس، تحتاج App Review من Meta للصلاحيات.