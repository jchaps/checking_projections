#!/usr/bin/env bash
set -e

echo "Checking Projections - Install"
echo "==============================="
echo

# Create directories
mkdir -p data secrets

# Create .env if missing
if [ ! -f .env ]; then
  touch .env
  echo "Created .env"
else
  echo ".env already exists, skipping"
fi

# Create secrets/plaid_tokens.json if missing
if [ ! -f secrets/plaid_tokens.json ]; then
  echo '{}' > secrets/plaid_tokens.json
  chmod 600 secrets/plaid_tokens.json
  echo "Created secrets/plaid_tokens.json"
else
  echo "secrets/plaid_tokens.json already exists, skipping"
fi

# Create config.yaml if missing
if [ ! -f config.yaml ]; then
  cat > config.yaml <<'EOF'
plaid:
  environment: development

accounts:
  checking:
    plaid_item: ""
    account_name: ""
  credit_cards: []

smtp:
  host: smtp.gmail.com
  port: 465
  username: ""
  password: ""
  from: ""
  to: ""

thresholds:
  low_balance_warning: 5000

schedule:
  sync:
    days: "mon,wed,sat"
    hour: 10
    minute: 0
  digest:
    days: "sat"
    hour: 10
    minute: 5

digest:
  projection_days_detail: 30
  projection_days_lowpoint: 30

data_dir: ./data
EOF
  echo "Created config.yaml"
else
  echo "config.yaml already exists, skipping"
fi

# Create recurring.yaml if missing
if [ ! -f recurring.yaml ]; then
  cat > recurring.yaml <<'EOF'
transactions: []
EOF
  echo "Created recurring.yaml"
else
  echo "recurring.yaml already exists, skipping"
fi

# Build image if it doesn't exist yet
IMAGE=$(docker compose config --images 2>/dev/null | head -1)
if [ -z "$(docker images -q "$IMAGE" 2>/dev/null)" ]; then
  echo
  echo "Building Docker image..."
  docker compose build
else
  echo
  echo "Docker image already built, skipping (run 'docker compose build' to rebuild)"
fi

# Stop running application if needed
if docker compose ps --status running 2>/dev/null | grep -q checking-projections; then
  echo
  echo "Stopping running application..."
  docker compose down
fi

echo
echo "Starting setup wizard..."
echo

# Run the wizard in the background, then open the browser once it's ready
docker compose run --rm -p 8485:8485 checking-projections setup &
WIZARD_PID=$!

# Wait for the wizard to be reachable
until curl -s -o /dev/null http://localhost:8485/ 2>/dev/null; do
  sleep 0.5
done

# Open in default browser (macOS: open, Linux: xdg-open)
if command -v open &>/dev/null; then
  open http://localhost:8485
elif command -v xdg-open &>/dev/null; then
  xdg-open http://localhost:8485
else
  echo "Open http://localhost:8485 in your browser to complete setup."
fi

echo "Complete the setup in your browser."
echo

# Wait for the wizard to exit (user clicks "Finish & Close")
wait $WIZARD_PID 2>/dev/null || true

echo
echo "Setup complete. Starting application..."
docker compose up -d

echo
echo "Checking Projections is now running."
echo "View logs with: docker compose logs -f"
echo
