# Deploy on Hostinger VPS (alongside existing apps)

This stack is designed **not** to fight your existing Next.js / Mongo setup:

| Service | Bind | Notes |
|---------|------|--------|
| Flask API | `127.0.0.1:5001` | Avoids common `:5000` |
| Next dashboard | `127.0.0.1:3001` | Avoids your other app on `:3000` |
| MongoDB | existing | Uses DB name `content_detector` |
| Nginx | subdomain | New site file only |

## One-shot install (on the VPS)

```bash
sudo apt-get update -y && sudo apt-get install -y git
git clone https://github.com/trisergetechnologies/piracy-detector.git /tmp/piracy-setup
cd /tmp/piracy-setup

export DOMAIN=piracy.yourdomain.com   # change me
export REPO_URL=https://github.com/trisergetechnologies/piracy-detector.git
sudo -E bash deploy/setup.sh
```

Then:

```bash
sudo nano /var/www/piracy-detector/.env
# YOUTUBE_API_KEYS=...
# OPENAI_API_KEY=...
# MONGODB_URI=mongodb://127.0.0.1:27017/

sudo systemctl restart piracy-api piracy-ui
sudo certbot --nginx -d piracy.yourdomain.com
curl -s http://127.0.0.1:5001/health
```

Point DNS **A record** `piracy.yourdomain.com` → VPS public IP.

## Update later

```bash
cd /var/www/piracy-detector
sudo git pull
sudo ./venv/bin/pip install -r requirements.txt
cd dashboard && sudo npm ci && sudo NEXT_PUBLIC_API_URL=http://127.0.0.1:5001 npm run build && cd ..
sudo systemctl restart piracy-api piracy-ui
```

## Telegram (optional)

```bash
cd /var/www/piracy-detector
sudo -u www-data bash -c 'source venv/bin/activate && python scripts/telegram_login.py'
sudo systemctl restart piracy-api
```

## Useful commands

```bash
sudo systemctl status piracy-api piracy-ui
sudo journalctl -u piracy-api -f
sudo journalctl -u piracy-ui -f
```
