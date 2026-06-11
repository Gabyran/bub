#!/bin/bash

set -eo pipefail

if [ -f "/workspace/bub-reqs.txt" ]; then
    echo "Installing additional requirements from /workspace/bub-reqs.txt"
    uv pip install -r /workspace/bub-reqs.txt -p /app/.venv/bin/python
fi

export BUB_PROJECT="${BUB_PROJECT:-/workspace/bub-project}"
source /app/.venv/bin/activate

if [ -x "/app/.venv/bin/python" ]; then
    cat > /app/.venv/bin/nmem <<'EOF'
#!/bin/bash
set -euo pipefail

args=()
for arg in "$@"; do
    if [ "$arg" = "--json" ]; then
        args+=("-j")
    else
        args+=("$arg")
    fi
done

exec /app/.venv/bin/python -m nowledge_graph_server.ncli "${args[@]}"
EOF
    chmod +x /app/.venv/bin/nmem
fi

/app/.venv/bin/bub install
if [ -f "/workspace/startup.sh" ]; then
    exec bash /workspace/startup.sh
else
    exec /app/.venv/bin/bub gateway
fi
