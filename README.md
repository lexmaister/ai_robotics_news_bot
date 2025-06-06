# AI & Robotics News Feed Bot

A **self-hosted, fully automated Telegram bot** that curates and delivers the most interesting, unusual, and high-quality news on Artificial Intelligence (AI) and Robotics directly to the [AI and Robotics News](https://t.me/robotics_ai_news) Telegram Channel. Built for advanced tech enthusiasts and professionals, the bot collects news from leading sources, applies strict editorial rules, and posts concise updates every few hours — all with zero code required, thanks to n8n automation.

## Features
- **Multi-source Aggregation**: Gathers news from multiple sources globally via [newsdata.io](https://newsdata.io/).
- **AI-Powered Editorial Selection**: Uses a Large Language Model (LLM) to select the most relevant and unique articles on AI and robotics every 2 hours.
- **Strict Editorial Filtering**: Filters out irrelevant, purely scientific, product/finance, entertainment, or PR news; focuses on real-world, diverse, impactful developments.
- **Unusual & Fun Picks**: Includes up to one "funny" or odd story per cycle to keep the feed engaging.
- **Fully Automated Telegram Posting**: Publishes summaries with links to the Telegram channel using Markdown formatting.
- **Multilingual & Multiregional Coverage**: Tracks both English and Russian sources, with a global focus.
- **Zero Code Operation**: Managed via [n8n](https://n8n.io/) workflows—**no coding required** for daily operation.
---
## Architecture Overview
| Component             | Description                                   |
|-----------------------|-----------------------------------------------|
| **n8n (Docker)**      | Orchestrates all automation workflows         |
| **MySQL Database**    | Stores and de-duplicates news articles        |
| **newsdata.io API**   | Main source for global news aggregation       |
| **Mistral LLM**       | AI-based selection and logic for top news     |
| **Telegram Bot API**  | Publishes news to the Telegram channel        |
---

**Project workflow**
![newsdata_io_full.png](./docs/newsdata_io_full.png)

## Installation
### 1. Prerequisites
- Linux server (Ubuntu recommended)
- Docker and Docker Compose
- SSH access
- Telegram bot token (for posting to your channel and send error messages)
- [newsdata.io](https://newsdata.io/) API key
- [Mistral](https://mistral.ai/) API key

### 2. Remote connection to Linux server with RDP via SSH tunnel

To connect to your remote host with `n8n` via SSH tunnel, you can use Remmina (or any other RDP client) to create a secure connection. RDP (Remote Desktop Protocol) allows you to access the graphical interface of your remote server with its browser to manage and monitor your `n8n` instance.

#### Installing XORG and XRDP on remote host

Use Xfce as a lightweight, compatible desktop:
```sh
sudo apt update && sudo apt upgrade -y
sudo apt install xorg -y
sudo apt install xrdp -y
sudo apt install xfce4 xfce4-goodies -y
echo "startxfce4" > ~/.xsession
sudo systemctl enable xrdp
sudo systemctl start xrdp
```

Reboot and check if your RDP is working:
```sh
sudo systemctl status xrdp
```

#### SSH Tunnel for RDP Connection

On local machine (client)
```sh
ssh -L 3389:localhost:3389 lexmaister@176.123.163.200
```

Then connect to RDP via `localhost:3389`

You can also use Remmina’s built-in SSH tunnel feature instead of manually opening an SSH session in a separate terminal.

How to Use This Feature
* Enable SSH tunnel by checking the box.
* Select "Same server at port 22" if your SSH and RDP run on the same host.
* Enter your authentication details:
  * Set authentication type (SSH identity file for key-based auth, Password if using password login).
  * Username: your SSH username
  * SSH private key file: path to your key (e.g., ~/.ssh/id_rsa, or your Ed25519 key)
  * Password to unlock private key: fill if your key is passphrase-protected
* Save and Connect.

Remmina will create the SSH tunnel and then start your secure RDP session automatically.

### 3. Setup n8n (Self-hosted with Docker Compose)
Install Docker and Docker Compose [for n8n](https://docs.n8n.io/hosting/installation/server-setups/docker-compose/#1-install-docker-and-docker-compose):
```sh
# Remove incompatible or out of date Docker implementations if they exist
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do sudo apt-get remove $pkg; done
# Install prereq packages
sudo apt-get update
sudo apt-get install ca-certificates curl
# Download the repo signing key
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
# Configure the repository
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Update and install Docker and Docker Compose
sudo apt-get update
sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Clone project directory and create data storage:
```sh
git clone https://github.com/lexmaister/ai_robotics_news_bot.git
cd ai_robotics_news_bot
mkdir -p ./data/n8n ./data/mysql
```

Copy `n8n_mysql-compose.yaml` to `docker-compose.yaml` (in the main repo folder):
```sh
cp n8n_mysql-compose.yaml docker-compose.yaml
```
and set passwords for Mysql `root` and `n8n_user` in `docker-compose.yaml` that will be used by n8n.

Start containers:
```sh
docker compose up -d
```

Check containers are running:
```sh
docker ps
```

### 4. Setup MySQL Database

Connect to the Mysql container as admin using set password from `docker-compose.yaml`:
```sh
docker exec -it n8n-mysql-1 mysql -u root -p
```

Show databases:
```sql
SHOW DATABASES; 
```

There should be `n8n_news` in the list.

Grant priviledges to `n8n_user`:
```sql
GRANT ALL PRIVILEGES ON n8n_news.* TO 'n8n_user'@'%';
FLUSH PRIVILEGES;
```

Exit then connect to the Mysql as user `n8n_user` to `n8n_news`:
```sh
docker exec -it n8n-mysql-1 mysql -u n8n_user -p n8n_news
```

Create table for newsdata.io news:
```sql
CREATE TABLE newsdata_io (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    article_id VARCHAR(64) NOT NULL,
    title VARCHAR(704) NOT NULL,
    description TEXT NOT NULL,
    link VARCHAR(1024) NOT NULL,
    source_priority INT,
    category JSON,
    pub_dt DATETIME NOT NULL,
    collected_dt DATETIME NOT NULL,
    posted_dt DATETIME,
    CONSTRAINT UC_newsdata UNIQUE (article_id,title)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SHOW TABLES;
```

Output should be like:
```
+--------------------+
| Tables_in_n8n_news |
+--------------------+
| newsdata_io        |
+--------------------+
```

### 5. Import & Configure n8n Workflows

* Open `http://localhost:5678` and log into n8n.
* Create two blank workflows, then import JSON files (`newsdata_io.json`, `error_handler.json`) from `./workflows/` and rename them accordingly to:
  * `Newsdata.io`,
  * `Error Handler`.

![import_workflow](./docs/import_workflow.png)  

* Create and connect credentials:
  * `Query Auth` for newsdata.io API key (name: `apikey`)
  * `MySQL` DB credentials (hostname: `mysql`)
  * `Mistral` API key (for LLM)
  * `Telegram` Bot API key

![credentials](./docs/credentials.png)

Set workflows' settings as follows:
* Error Workflow: specify `error_handler` in `newsdata_io` workflow settings, see [more](https://docs.n8n.io/flow-logic/error-handling/#create-and-set-an-error-workflow)
* Timezone: `UTC` (to be compatible with newsdata.io publication datetime)
* Executions Saving: off or minimal, for performance

Test `newsdata_io` workflow manually, then activate both workwlows for full automation.

## How It Works

### Scheduled Trigger
Every 2 hours, the workflow automatically checks for new articles from the past 48 hours that have not been posted.

### News Aggregation
Articles are gathered using the newsdata.io API with a focus on AI, robotics, and global coverage. The system filters incoming articles to remove duplicates and avoid reposting.

### Editorial Selection
Articles are summarized and passed to an AI-based agent powered by the Mistral LLM, which selects up to the top 3 most relevant articles based on strict editorial guidelines.

### Publishing
For each selected article:
* It is marked as "posted" in the database.
* A formatted Markdown post is published to the [AI and Robotics News](https://t.me/robotics_ai_news) Telegram channel via the Telegram Bot API.

### Error Handling
Any issues, such as failed API calls or empty responses, are managed through an `Error Handler` workflow that logs errors and sends alerts to Telegram.

### Editorial Guidelines
* Priority: Articles emphasizing real-world applications and significant impact.
* Scope: Covers both AI and robotics topics, ensuring diversity in sources, topics, and regions.
* Exclusions: Content related to finance, PR, entertainment, celebrities, or product launches. Pure academic research without practical applications is omitted.
* Selection Criteria: Includes global impact, innovative findings, ethical discussions, and one "fun" or surprising news piece per cycle.

### Workflow Summary
Aggregates news automatically, filters articles, and ensures fully automated publishing.
Operates on predefined rules for top-quality, impactful, and engaging content.

## Maintenance & Troubleshooting
* Extend sources: Tweak queries in the Endpoint Generator node to broaden or narrow search.
* Credentials: Rotate API keys regularly.
* Workflow logs: Check n8n’s logs (docker logs n8n) and manual runs for issues.
* Database: Regularly back up n8n_news database.

## License
This project is licensed under the MIT License. See the [LICENSE](./LICENSE) file for details.

## Author / Credits
[@lexmaister](https://t.me/lexmaister)
[@AI & Robotics Lab](https://t.me/ai_code_developer)
[@AI & Robotics News](https://t.me/robotics_ai_news)

For a behind-the-scenes walkthrough, see messages [106](https://t.me/ai_code_developer/106), [109](https://t.me/ai_code_developer/109) and following in the [@AI & Robotics Lab](https://t.me/ai_code_developer).
