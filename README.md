# 💜 Vynora Live

Vynora Live is a Telegram Mini App for private 1-to-1 video call booking between Users and approved Hosts.

## 🚀 Main Features

- Telegram Mini App
- User Dashboard
- Host Dashboard
- Admin Dashboard
- Real Host listing
- Host online/offline status
- Video call booking
- Agora 1-to-1 video calling
- Wallet / Coins
- UPI / QR Recharge
- Recharge approval system
- Host earnings
- Withdrawal system
- Telegram notifications
- Booking history
- Call history
- Admin commands

## 💰 Call & Coin System

The current introductory plan is:

- 1 Minute = 20 Coins
- The 1-minute / 20-Coins offer can be used only once per Telegram User ID.
- It becomes consumed after the call actually connects.
- An unsuccessful, rejected, cancelled, or non-connected booking must not consume the offer.

Other available booking durations depend on the current booking-plan configuration.

## 📞 Call Timer Rule

The paid call timer must NOT start when:

- Host only accepts the booking
- Host only opens the call screen
- User only opens the call screen
- Telegram notification is sent

The timer starts only after:

1. User joins the call.
2. Host joins the call.
3. Backend receives both connection signals.
4. Backend sets the server-side `call_started_at`.
5. The connected-call timer begins.

Call settlement uses the actual connected duration.

## 💸 Host / Platform Split

Current revenue distribution:

- Host: 60%
- Platform: 40%

The same 60/40 rule is used for call settlement and host earnings.

## 💳 Recharge System

Users can select a recharge plan, enter the UTR / transaction ID and upload the payment screenshot.

Recharge flow:

1. User selects recharge plan.
2. User enters UTR.
3. User uploads payment screenshot.
4. Recharge request is created as `pending`.
5. Admin receives the request in Group 1.
6. Admin approves or rejects the request.
7. User receives an approval/rejection notification.
8. Approved Coins are credited only once.

### Duplicate Protection

A user must not be able to create multiple identical pending recharge requests before the existing request is approved or rejected.

Admin recharge approval is also protected against duplicate processing.

A second approval click must not credit Coins again.

## 👥 Telegram Groups

### Group 1 — Finance / Recharge

Used for:
- Recharge requests
- Recharge approval/rejection
- Finance-related actions

Call completion activity should NOT be sent here.

### Group 2 — New Users

Used only for:
- New user registration notifications

Group 2 should NOT receive:
- Recharge requests
- Recharge approvals
- Booking activity
- Call activity
- Withdrawal activity
- Host operational logs
- Offers
- Announcements

### Group 3 — Operations

Used for operational activity such as:
- Booking requests
- Booking accepted/rejected
- Call ready
- Call connected
- Call completed
- Actual call duration
- Host activity
- Recharge final events
- Withdrawal final events
- Relevant admin activity

## 👤 User Dashboard

User features include:
- Home
- Find Host
- Real Hosts
- Host Profile
- Book Slot
- My Bookings
- Wallet
- Recharge
- Notifications
- Profile
- Support
- Call Screen
- Booking History
- Call History

## 🎙️ Host Dashboard

Approved Hosts can access:
- Host Profile
- Online / Offline
- Booking Requests
- Accept / Reject
- Schedule
- Upcoming Calls
- Call History
- Earnings
- Gift Earnings
- Withdrawal
- Camera
- Face Filters
- Filter Settings
- Profile Settings

Host access requires valid Telegram Mini App authentication.

## 👑 Admin

Admin functions include commands for:
- Token management
- Host approval
- Host rejection/removal
- User approval
- User information
- Ban / Unban
- Block / Unblock
- Recharge approval/rejection
- Booking approval/rejection
- Announcements
- Offers
- Direct admin calls
- Country management
- Statistics

Use `/helpadmin` to view the available Admin commands.

## 🔐 Security

Telegram Mini App authentication uses Telegram `initData`.

The backend verifies the Telegram Mini App session using the Bot Token.

Do not trust a URL `user_id` as authentication.

Production credentials must NOT be placed inside frontend files or committed to GitHub.

Keep these values in environment variables:

```text
BOT_TOKEN
MONGO_URI
AGORA_APP_ID
AGORA_APP_CERTIFICATE
GROUP_1_ID
GROUP_2_ID
GROUP_3_ID
ADMIN_IDS
HOST_SHARE
WEB_APP_URL
```

## 🗄️ Database

Vynora Live uses MongoDB.

Important collections include:

```text
users
hosts
bookings
recharges
withdrawals
notifications
direct_calls
settings
```

The database stores:
- User Coins
- Booking history
- Recharge history
- Host earnings
- Withdrawal history
- Call timestamps
- Call duration
- Settlement status
- Notifications

## 🛡️ Duplicate Money Protection

### Recharge

Only the first valid admin action can change:

```text
pending → approved
```

or:

```text
pending → rejected
```

A second admin action must not credit Coins again.

### Call Settlement

Call settlement must happen only once.

The system must prevent:
- Duplicate Host earnings
- Duplicate User refunds
- Duplicate Platform revenue

### Withdrawal

Withdrawal approval/rejection must also be processed only once.

## 🌐 Render Deployment

Build command:

```bash
pip install -r requirements.txt
```

For the FastAPI/MongoDB build, use:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## 📁 Required Project Files

```text
main.py
index.html
requirements.txt
README.md
```

Branding assets where used:

```text
vynora-icon.png
vynora-banner.png
favicon.ico
```

## ⚙️ Environment Variables

Example:

```text
BOT_TOKEN=YOUR_BOT_TOKEN
MONGO_URI=YOUR_MONGODB_URI
MONGO_DB=vynora_live

GROUP_1_ID=YOUR_GROUP_1_ID
GROUP_2_ID=YOUR_GROUP_2_ID
GROUP_3_ID=YOUR_GROUP_3_ID

AGORA_APP_ID=YOUR_AGORA_APP_ID
AGORA_APP_CERTIFICATE=YOUR_AGORA_APP_CERTIFICATE

ADMIN_IDS=YOUR_ADMIN_ID,YOUR_SECOND_ADMIN_ID

HOST_SHARE=0.60

WEB_APP_URL=https://vynora-live-new.onrender.com
```

Never commit real production credentials to GitHub.

## 🧪 Final Testing Checklist

### User
- `/start`
- Open Vynora Live
- Telegram authentication
- Host list
- Host profile
- Wallet
- Recharge
- Booking
- Booking history

### Recharge
- Select plan
- Enter UTR
- Upload screenshot
- Submit
- Confirm Group 1 request
- Admin approval
- Coins credited exactly once
- User receives notification
- Group 3 receives final event
- Group 2 receives nothing

### 1-Minute Offer
- 1-minute plan shows 20 Coins
- First eligible user can use it
- Call connects
- Both participants connect
- Timer starts
- After successful connection, the same Telegram User ID cannot use the 1-minute / 20-Coins offer again
- Other booking plans remain available

### Paid Call
- User books
- Coins are reserved/deducted
- Host accepts
- User joins
- Host joins
- Timer starts only after both connect
- Call ends
- Actual duration is stored
- Correct Coins are charged
- Unused Coins are refunded where applicable
- Host receives 60%
- Platform receives 40%
- Settlement happens only once

### Duplicate Protection
Test:
- Double-click recharge submit
- Double-click admin recharge approval
- Double-submit booking
- Repeated call-end request
- Repeated withdrawal action

No duplicate Coins, refunds or earnings should occur.

## 💜 Vynora Live

Secure • Private • 1-to-1 Video Calling

Real Connections • Real Moments
