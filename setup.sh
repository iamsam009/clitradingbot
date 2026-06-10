#!/bin/bash
# =============================================================================
# setup.sh - Bollinger Band Reversal Bot Setup & Launcher (Ubuntu)
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh              # Install deps + setup + start in background
#   ./setup.sh logs         # Tail real-time logs
#   ./setup.sh stop         # Stop the background bot
#   ./setup.sh status       # Check if bot is running
#   ./setup.sh restart      # Restart the bot
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/.bot.pid"
LOG_FILE="$SCRIPT_DIR/bot.log"
SETUP_DONE_FILE="$SCRIPT_DIR/.setup_done"

# ─── Colors ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

print_banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║     🤖 BOLLINGER BAND REVERSAL TRADING BOT              ║"
    echo "  ║     5-Min BTC/USDT | SharkEx | INR Risk Limits          ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ─── Install Dependencies ──────────────────────────────────────────────────
install_deps() {
    echo -e "${BOLD}[1/4]${NC} Checking Python3..."
    if ! command -v python3 &> /dev/null; then
        echo -e "${RED}Python3 not found! Installing...${NC}"
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip python3-venv
    fi
    echo -e "  ${GREEN}✔${NC} Python3: $(python3 --version)"

    echo -e "${BOLD}[2/4]${NC} Creating virtual environment..."
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        echo -e "  ${GREEN}✔${NC} Virtual environment created"
    else
        echo -e "  ${YELLOW}○${NC} Virtual environment already exists"
    fi

    echo -e "${BOLD}[3/4]${NC} Installing Python packages..."
    source venv/bin/activate
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    echo -e "  ${GREEN}✔${NC} All packages installed"

    echo -e "${BOLD}[4/4]${NC} Verifying imports..."
    python3 -c "
from config import BotConfig; 
from exchange_client import SharkExClient, BinanceFuturesClient, create_exchange_client;
from strategy import BollingerBandStrategy;
from risk_manager import RiskManager;
from cli_display import CLIDisplay;
from bot import TradingBot;
print('  ✔ All modules loaded successfully')
" 2>&1
    echo ""
}

# ─── Ask for API Credentials ───────────────────────────────────────────────
ask_credentials() {
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  🔑 SHARKEX API CREDENTIALS${NC}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  Get your API keys from: ${CYAN}https://sharkexchange.in${NC}"
    echo ""

    read -p "  SharkEx API Key    : " API_KEY
    read -s -p "  SharkEx API Secret  : " API_SECRET
    echo ""
    echo ""

    if [ -z "$API_KEY" ] || [ -z "$API_SECRET" ]; then
        echo -e "  ${RED}✖ API Key and Secret are required!${NC}"
        exit 1
    fi

    # Write .env file
    cat > "$SCRIPT_DIR/.env" << ENVEOF
# Bollinger Band Reversal Bot - Configuration
SHARKEX_API_KEY=${API_KEY}
SHARKEX_API_SECRET=${API_SECRET}
EXCHANGE_NAME=sharkex
SYMBOL=BTC/USDT
BB_PERIOD=20
BB_STD_DEV=2.0
NEAR_THRESHOLD=0.002
TRAIL_PCT=0.005
TRADE_SIZE_INR=20000
USD_INR_RATE=83.5
MAX_DAILY_LOSS_INR=3000
MAX_TRADES_PER_DAY=30
POLL_INTERVAL=60
PAPER_TRADING=false
ENVEOF

    echo -e "  ${GREEN}✔${NC} Credentials saved to .env"
    echo ""

    # Copy paper trading request
    read -p "  Run in paper trading mode? (y/N): " PAPER_CHOICE
    if [[ "$PAPER_CHOICE" =~ ^[Yy]$ ]]; then
        sed -i 's/PAPER_TRADING=false/PAPER_TRADING=true/' "$SCRIPT_DIR/.env"
        echo -e "  ${YELLOW}📝${NC} Paper trading mode enabled (no real orders)"
    fi
    echo ""
}

# ─── Start Bot in Background ───────────────────────────────────────────────
start_bot() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo -e "${YELLOW}⚠ Bot is already running! (PID: $(cat $PID_FILE))${NC}"
        echo -e "  Use: ${BOLD}./setup.sh logs${NC} to view logs"
        echo -e "  Use: ${BOLD}./setup.sh stop${NC} to stop"
        return 1
    fi

    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  🚀 STARTING BOT IN BACKGROUND${NC}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    source "$SCRIPT_DIR/venv/bin/activate"

    # Clear stale Python bytecode cache to ensure code changes take effect
    find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

    # Start bot as a daemon — bot handles its own PID file and logging
    # PYTHONDONTWRITEBYTECODE=1 prevents future .pyc cache issues
    env PYTHONDONTWRITEBYTECODE=1 python3 "$SCRIPT_DIR/bot.py" --no-interactive --daemon &
    # Wait for daemon to fork and write PID file
    sleep 2
    BOT_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")

    if kill -0 $BOT_PID 2>/dev/null; then
        echo -e "  ${GREEN}✔ Bot started successfully!${NC}"
        echo -e "  ${GREEN}  PID:${NC} $BOT_PID"
        echo -e "  ${GREEN}  Log:${NC} $LOG_FILE"
        echo ""
        echo -e "  ${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "  ${BOLD}  📋 REAL-TIME LOGS (last 30 lines):${NC}"
        echo -e "  ${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        tail -n 30 "$LOG_FILE"
        echo ""
        echo -e "  ${BOLD}▶ Live tail starting...${NC} (press ${YELLOW}Ctrl+C${NC} to exit logs, bot keeps running)"
        echo -e "  Use ${BOLD}./setup.sh logs${NC} to view logs anytime"
        echo ""
        tail -f "$LOG_FILE"
    else
        echo -e "  ${RED}✖ Bot failed to start!${NC}"
        echo -e "  ${RED}  Check logs:${NC} cat $LOG_FILE"
        cat "$LOG_FILE" | tail -n 20
        rm -f "$PID_FILE"
        exit 1
    fi
}

# ─── Show Logs ─────────────────────────────────────────────────────────────
show_logs() {
    if [ ! -f "$LOG_FILE" ]; then
        echo -e "${RED}No log file found. Bot may not have run yet.${NC}"
        exit 1
    fi

    echo -e "${BOLD}📋 Real-time logs (Ctrl+C to exit, bot keeps running):${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    tail -n 30 "$LOG_FILE"
    echo ""
    tail -f "$LOG_FILE"
}

# ─── Stop Bot ──────────────────────────────────────────────────────────────
stop_bot() {
    if [ ! -f "$PID_FILE" ]; then
        echo -e "${YELLOW}No PID file found. Bot is not running.${NC}"
        return 0
    fi

    BOT_PID=$(cat "$PID_FILE")
    if kill -0 $BOT_PID 2>/dev/null; then
        echo -e "${YELLOW}Stopping bot (PID: $BOT_PID)...${NC}"
        kill $BOT_PID 2>/dev/null
        sleep 2

        # Force kill if still running
        if kill -0 $BOT_PID 2>/dev/null; then
            echo -e "${RED}Bot did not stop. Force killing...${NC}"
            kill -9 $BOT_PID 2>/dev/null
        fi
        echo -e "${GREEN}✔ Bot stopped${NC}"
    else
        echo -e "${YELLOW}Bot (PID: $BOT_PID) is no longer running${NC}"
    fi

    rm -f "$PID_FILE"
}

# ─── Check Status ──────────────────────────────────────────────────────────
check_status() {
    if [ ! -f "$PID_FILE" ]; then
        echo -e "${RED}Bot is NOT running (no PID file)${NC}"
        return 1
    fi

    BOT_PID=$(cat "$PID_FILE")
    if kill -0 $BOT_PID 2>/dev/null; then
        echo -e "${GREEN}✔ Bot is RUNNING${NC}"
        echo -e "  PID: $BOT_PID"
        echo -e "  Log: $LOG_FILE"

        # Show recent activity
        if [ -f "$LOG_FILE" ]; then
            LAST_LINE=$(tail -1 "$LOG_FILE" 2>/dev/null)
            echo -e "  Last log: ${CYAN}${LAST_LINE}${NC}"
        fi
    else
        echo -e "${RED}Bot is NOT running (stale PID: $BOT_PID)${NC}"
        rm -f "$PID_FILE"
        return 1
    fi
}

# ─── One-Time Setup (if not already done) ──────────────────────────────────
run_setup_if_needed() {
    if [ ! -f "$SETUP_DONE_FILE" ]; then
        print_banner
        install_deps
        ask_credentials
        touch "$SETUP_DONE_FILE"
        echo -e "${GREEN}✔ Setup complete!${NC}"
        echo ""
    fi
}

# ─── Main ──────────────────────────────────────────────────────────────────
case "${1:-}" in
    logs)
        show_logs
        ;;
    stop)
        stop_bot
        ;;
    status)
        check_status
        ;;
    restart)
        echo -e "${YELLOW}Restarting bot...${NC}"
        stop_bot
        sleep 1
        source "$SCRIPT_DIR/venv/bin/activate"
        # Clear stale bytecode cache before restart
        find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
        env PYTHONDONTWRITEBYTECODE=1 python3 "$SCRIPT_DIR/bot.py" --no-interactive --daemon &
        sleep 2
        echo -e "${GREEN}✔ Bot restarted (PID: $(cat "$PID_FILE" 2>/dev/null || echo 'N/A'))${NC}"
        echo ""
        tail -n 20 "$LOG_FILE"
        ;;
    *)
        # Default flow: setup (if needed) + start
        run_setup_if_needed
        print_banner
        start_bot
        ;;
esac