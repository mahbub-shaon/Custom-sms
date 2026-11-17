# CustomSMS / CarryBee Merchant Bot

একটি config-driven Telegram বট যা CarryBee merchant registration, point wallet,
referral এবং সম্পূর্ণ admin panel পরিচালনা করে।

## ফাইল স্ট্রাকচার

```
customsms/
├── main.py                 # সম্পূর্ণ বট (single entry point)
├── config.json             # সব সেটিংস (TOKEN সহ) — গোপন রাখুন
├── requirements.txt        # PyTelegramBotAPI, requests, urllib3
├── bot_database.db         # লাইভ SQLite DB (১১৯ ইউজার)
├── backups/                # স্বয়ংক্রিয়/ম্যানুয়াল DB ব্যাকআপ
├── .gitignore              # config.json ও DB commit হওয়া থেকে সুরক্ষা
└── README.md
```

## ১. config.json পূরণ করা

```json
{
  "bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
  "admin": {
    "telegram_id": 123456789,
    "name": "Admin Name",
    "contact_username": "@YourUsername",
    "contact_url": "https://t.me/YourUsername"
  },
  "developer": {
    "name": "Developer Name",
    "username": "@DeveloperUsername",
    "url": "https://t.me/DeveloperUsername",
    "note": "Official support contact."
  },
  "force_join": {
    "enabled": true,
    "channels": [
      { "username": "@channel_one", "label": "Channel One", "url": "https://t.me/channel_one" },
      { "username": "@channel_two", "label": "Channel Two", "url": "https://t.me/channel_two" }
    ]
  },
  "merchant_api": {
    "url": "https://api-merchant.carrybee.com/api/v2/merchant/register",
    "timeout": 30
  },
  "pricing": [
    { "points": 10, "price": 30 },
    { "points": 30, "price": 60 },
    { "points": 50, "price": 100 }
  ]
}
```

### গুরুত্বপূর্ণ নিয়ম

- `admin.telegram_id` অবশ্যই **সংখ্যা** (numeric Telegram ID) হবে। Username দিয়ে
  কেউ অ্যাডমিন হতে পারবে না।
- `force_join.enabled`:
  - `true` → সব চ্যানেলে জয়েন করতে হবে।
  - `false` → কোনো Membership Verification ছাড়াই বট ব্যবহার করা যাবে।
- `force_join.channels`-এ যত খুশি চ্যানেল যোগ/বাদ দেওয়া যাবে। প্রতিটি entry
  string (`"@channel"`), পূর্ণ লিংক (`"https://t.me/channel"`) অথবা
  `{ "username": ..., "label": ..., "url": ... }` অবজেক্ট হতে পারে।
- চ্যানেল পরিবর্তন করতে কোড এডিট করতে হবে না — শুধু `config.json` বদলান।
- **গুরুত্বপূর্ণ:** Force Join চালু থাকলে বটকে অবশ্যই প্রতিটি চ্যানেলের member
  (ভেরিফিকেশনের জন্য admin হওয়া উত্তম) হতে হবে, নাহলে Telegram
  `getChatMember` ফেইল করবে এবং ইউজার ভেরিফাই করতে পারবে না।

## ২. চালানোর পদ্ধতি

### Windows (PowerShell)

```powershell
cd C:\Users\Rayhan\Desktop\customsms
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

### Linux / VPS

```bash
cd /path/to/customsms
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

প্রয়োজনে কনফিগ/DB পাথ environment variable দিয়ে বদলানো যায়:

```powershell
$env:BOT_CONFIG = "D:\secrets\customsms-config.json"
$env:BOT_DB = "D:\data\bot_database.db"
python main.py
```

কোনো নতুন dependency লাগবে না। `requirements.txt`-এর প্যাকেজগুলোই যথেষ্ট।

## ৩. Admin ব্যবহার

| কমান্ড / বাটন | কাজ |
|---|---|
| `🛡️ Admin Panel` বা `/admin` | শুধু numeric Admin ID, শুধু private chat |
| `👥 Users Info` বা `/users` | মোট/সক্রিয় ইউজার ও pagination তালিকা |
| `➕ Add Points` বা `/addpoints <ID> <Amount>` | পয়েন্ট যোগ (positive, max 1,000,000) |
| `➖ Remove Points` বা `/removepoints <ID> <Amount>` | পয়েন্ট কাটা |
| `📢 Broadcast` | Text/Photo/Video/Document/Voice/Audio/Animation/Sticker/Video Note |
| `🛑 Cancel Broadcast` | চলমান ব্রডকাস্ট থামানো |

ব্রডকাস্ট চলাকালে Sent / Failed / Remaining লাইভ দেখা যায়। Telegram 429
FloodWait স্বয়ংক্রিয়ভাবে সামলানো হয়, একজন ইউজার ব্লক করলেও বাকিদের কাছে
পাঠানো চলতে থাকে।

## ৪. ডাটা সুরক্ষা

- `init_db()` শুধুমাত্র **additive migration** করে:
  - `users` টেবিল অপরিবর্তিত থাকে (শুধু প্রয়োজন হলে `last_seen` কলাম যোগ হয়)।
  - নতুন `bot_states` ও `broadcast_jobs` টেবিল যোগ হয়।
- কোনো `DROP`, `DELETE FROM users`, বা point reset নেই।
- প্রথমবার নতুন কোড চালানোর আগে DB ব্যাকআপ নিন:

```powershell
Copy-Item bot_database.db "backups\bot_database.db.manual-$(Get-Date -Format yyyyMMdd_HHmmss).bak"
```

- রিস্টোর করতে চাইলে বট বন্ধ করে ব্যাকআপ ফাইলটি `bot_database.db` নামে কপি করুন।
- `config.json`, `bot_database.db` এবং `backups/` কখনো পাবলিক GitHub-এ দেবেন না
  (`.gitignore`-এ ইতিমধ্যে বাদ দেওয়া আছে)।
- Windows-এ শুধু নিজের অ্যাকাউন্টে `config.json` সীমাবদ্ধ করতে চাইলে:
  ```powershell
  icacls config.json /inheritance:r /grant:r "$env:USERNAME:F"
  ```

## ৫. নিরাপত্তা নোট

- Bot Token শুধু `config.json`-এ থাকে; কোডে হার্ডকোড নেই।
- Admin authorization শুধুমাত্র `admin.telegram_id` দিয়ে হয়।
- Admin Panel ও সব admin action শুধু private chat-এ কাজ করে; গ্রুপে কীবোর্ড leak হয় না।
- সব admin command/button/callback-এ সার্ভার-সাইড `require_admin()` চেক আছে।
- `requests` সর্বদা `verify=True` (TLS verification বন্ধ করার কোনো option নেই)।
- User-facing error গুলো generic; বিস্তারিত server log-এ যায়।
- যদি পুরনো `customsms.tar.gz` কোথাও শেয়ার হয়ে থাকে, BotFather-এ টোকেন
  **revoke** করে নতুন টোকেন `config.json`-এ বসান।
