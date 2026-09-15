name: meme-pump-hunter-v3

on:
  schedule:
    - cron: "0 */12 * * *"
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: hunter-v3
  cancel-in-progress: true

jobs:
  hunt:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 1

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Check configuration
        env:
          GOPLUS_APP_KEY: ${{ secrets.GOPLUS_APP_KEY }}
          GOPLUS_APP_SECRET: ${{ secrets.GOPLUS_APP_SECRET }}
          GMGN_API_KEY: ${{ secrets.GMGN_API_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          test -n "$GOPLUS_APP_KEY" && echo "GoPlus APP Key: configured" || echo "GoPlus APP Key: MISSING"
          test -n "$GOPLUS_APP_SECRET" && echo "GoPlus APP Secret: configured" || echo "GoPlus APP Secret: MISSING"
          test -n "$GMGN_API_KEY" && echo "GMGN API Key: configured" || echo "GMGN API Key: MISSING"
          test -n "$TELEGRAM_BOT_TOKEN" && echo "Telegram Bot Token: configured" || echo "Telegram Bot Token: MISSING"
          test -n "$TELEGRAM_CHAT_ID" && echo "Telegram Chat ID: configured" || echo "Telegram Chat ID: MISSING"

      - name: Hunt
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GOPLUS_APP_KEY: ${{ secrets.GOPLUS_APP_KEY }}
          GOPLUS_APP_SECRET: ${{ secrets.GOPLUS_APP_SECRET }}
          GMGN_API_KEY: ${{ secrets.GMGN_API_KEY }}
          NETWORKS: solana
          PRIMARY_NETWORK: solana
          MAX_AGE_H: 720
          GECKO_TOP_PAGES: 10
          GMGN_ENRICH_LIMIT: 10
          GMGN_WALLET_LIMIT: 2
          RUGCHECK_LIMIT: 15
        run: python hunter.py

      - name: Persist state
        run: |
          git config user.name "hunter-bot"
          git config user.email "actions@users.noreply.github.com"
          git add state/
          if git diff --cached --quiet; then
            echo "No state changes to commit."
          else
            git commit -m "hunter state update [skip ci]"
            git push
          fi
