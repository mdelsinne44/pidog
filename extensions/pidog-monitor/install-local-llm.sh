#!/bin/bash
# PiDog Monitor — installe le secours LLM hors ligne (llama.cpp + GGUF).
#
#   ./install-local-llm.sh            # construit llama-cli et télécharge le modèle
#   ./install-local-llm.sh --check    # vérifie sans rien modifier
#
# Choix : Qwen2.5-0.5B-Instruct Q4_K_M (~470 Mo). C'est le bon compromis sur
# Pi4 2 Go : assez gros pour suivre un schéma JSON, assez petit pour tenir en
# RAM à côté du reste (poids + KV cache ≈ 700 Mo), et le modèle n'est chargé
# que lorsque le réseau est tombé (monitor_ai.py est un process jetable).
#
# Pourquoi llama.cpp et pas ONNX : KV cache, sampler et grammaire GBNF intégrés,
# mmap du modèle (chargement à froid rapide, RSS maîtrisé) et optimisations
# NEON/ARM64. Le runtime ONNX est présent, mais générer avec un décodeur ONNX
# demande de gérer le cache à la main et reste plus lent ici.
#
# Variables : LLAMA_SRC, LLAMA_CLI, LLM_LOCAL_DIR, MON_MODEL_URL,
#             MON_MODEL_NAME, MON_MODEL_SHA256, MON_MODEL_MIN_BYTES.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
LLAMA_SRC="${LLAMA_SRC:-$HOME/src/llama.cpp}"
LLAMA_CLI="${LLAMA_CLI:-$HOME/.local/bin/llama-cli}"
MODEL_DIR="${LLM_LOCAL_DIR:-$HOME/models/llm-local}"
DEFAULT_MODEL_NAME="qwen2.5-0.5b-instruct-q4_k_m.gguf"
MODEL_NAME="${MON_MODEL_NAME:-$DEFAULT_MODEL_NAME}"
MODEL_URL="${MON_MODEL_URL:-https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/$DEFAULT_MODEL_NAME}"
MODEL_MIN_BYTES="${MON_MODEL_MIN_BYTES:-450000000}"
# SHA256 du fichier par défaut (vérifié auprès de l'API Hugging Face).
if [ -n "${MON_MODEL_SHA256:-}" ]; then
    MODEL_SHA256="$MON_MODEL_SHA256"
elif [ "$MODEL_NAME" = "$DEFAULT_MODEL_NAME" ]; then
    MODEL_SHA256="74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
else
    MODEL_SHA256=""
fi
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() { printf '%s\n' "$*"; }
die() { printf 'ERREUR : %s\n' "$*" >&2; exit 1; }
size_of() { stat -c%s "$1" 2>/dev/null || echo 0; }

say "== PiDog — secours LLM hors ligne =="
say "llama.cpp   : $LLAMA_SRC"
say "llama-cli   : $LLAMA_CLI"
say "modèle      : $MODEL_DIR/$MODEL_NAME"

if [ "$CHECK_ONLY" = 1 ]; then
    say ""
    if [ -x "$LLAMA_CLI" ]; then say "llama-cli   : présent"; else say "llama-cli   : ABSENT"; fi
    if [ -f "$MODEL_DIR/$MODEL_NAME" ]; then
        say "modèle      : présent ($(du -h "$MODEL_DIR/$MODEL_NAME" | cut -f1))"
    else
        say "modèle      : ABSENT"
    fi
    say "RAM dispo   : $(awk '/MemAvailable/{printf "%d Mo", $2/1024}' /proc/meminfo)"
    say "disque libre: $(df -h "$HOME" 2>/dev/null | tail -1 | awk '{print $4}' || echo '?')"
    "$HERE/monitor_ai.py" --offline --print-grammar >/dev/null && say "grammaire   : OK"
    if [ -x "$LLAMA_CLI" ]; then
        "$LLAMA_CLI" --version 2>&1 | head -1 | sed 's/^/llama-cli   : /'
    fi
    exit 0
fi

# ---------------------------------------------------------------- llama.cpp --
if [ -x "$LLAMA_CLI" ]; then
    say "→ llama-cli déjà installé, build sauté"
else
    for tool in git cmake make g++; do
        command -v "$tool" >/dev/null 2>&1 || die "$tool requis pour construire llama.cpp"
    done
    if [ ! -d "$LLAMA_SRC/.git" ]; then
        say "→ clone de llama.cpp (shallow)…"
        mkdir -p "$(dirname "$LLAMA_SRC")"
        git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_SRC" \
            || die "clone impossible"
    else
        say "→ sources déjà présentes, pas de mise à jour forcée"
    fi
    say "→ configuration cmake…"
    cmake -S "$LLAMA_SRC" -B "$LLAMA_SRC/build" \
        -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=ON \
        >/dev/null || die "cmake a échoué"
    # Sur 2 Go de RAM, 4 compilations parallèles peuvent déclencher l'OOM
    # killer : on plafonne à 2 jobs sous 3 Go de mémoire totale.
    JOBS="${MON_BUILD_JOBS:-}"
    if [ -z "$JOBS" ]; then
        MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
        if [ "$MEM_MB" -lt 3000 ]; then JOBS=2; else JOBS=$(nproc); fi
    fi
    say "→ compilation de llama-cli (jobs=$JOBS, patience ~5-10 min)…"
    cmake --build "$LLAMA_SRC/build" --target llama-cli -j"$JOBS" \
        || die "compilation impossible"
    BIN="$LLAMA_SRC/build/bin/llama-cli"
    [ -x "$BIN" ] || BIN=$(find "$LLAMA_SRC/build" -type f -name llama-cli -perm -u+x | head -1)
    [ -n "$BIN" ] && [ -x "$BIN" ] || die "binaire llama-cli introuvable après build"

    # Installation autonome : le binaire + ses .so sont copiés dans
    # ~/.local/lib/pidog-llama, et un wrapper fixe LD_LIBRARY_PATH. Ainsi
    # supprimer ~/src/llama.cpp ne casse pas le secours hors ligne.
    LIBDIR="$HOME/.local/lib/pidog-llama"
    install -d "$LIBDIR" || die "création de $LIBDIR impossible"
    cp -a "$LLAMA_SRC/build/bin/"lib*.so* "$LIBDIR"/ 2>/dev/null || true
    install -m 0755 "$BIN" "$LIBDIR/llama-cli" || die "copie du binaire impossible"
    mkdir -p "$(dirname "$LLAMA_CLI")"
    cat > "$LLAMA_CLI" <<EOF
#!/bin/sh
# Wrapper généré par install-local-llm.sh (libs dans ~/.local/lib/pidog-llama)
LD_LIBRARY_PATH="$LIBDIR\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}" exec "$LIBDIR/llama-cli" "\$@"
EOF
    chmod 0755 "$LLAMA_CLI" || die "installation du wrapper impossible"
    say "→ installé : $LLAMA_CLI (libs : $LIBDIR)"
fi

# ------------------------------------------------------------------- modèle --
mkdir -p "$MODEL_DIR" || die "création de $MODEL_DIR impossible"
DEST="$MODEL_DIR/$MODEL_NAME"
if [ -f "$DEST" ] && [ "$(size_of "$DEST")" -ge "$MODEL_MIN_BYTES" ]; then
    say "→ modèle déjà présent ($(du -h "$DEST" | cut -f1)), téléchargement sauté"
else
    say "→ téléchargement du modèle (~470 Mo)…"
    curl -L --fail --retry 3 -C - -o "$DEST.part" "$MODEL_URL" \
        || die "téléchargement impossible"
    mv "$DEST.part" "$DEST" || die "renommage impossible"
fi
if [ -n "$MODEL_SHA256" ]; then
    say "→ vérification SHA256…"
    echo "$MODEL_SHA256  $DEST" | sha256sum -c - || die "SHA256 invalide"
fi
[ "$(size_of "$DEST")" -ge "$MODEL_MIN_BYTES" ] \
    || die "modèle trop petit ou corrompu ($(size_of "$DEST") octets)"

# ------------------------------------------------------------------ smoke ----
say ""
say "== test à froid =="
"$LLAMA_CLI" --version 2>&1 | head -2 || true
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
printf 'reseau:wifi\t0\tcritical\twlan0 déconnecté\nreseau:wan\t0\tcritical\tinjoignable\n' > "$TMP/checks.tsv"
printf 'temp_c\t50\nram_mb\t850\n' > "$TMP/hw.tsv"
: > "$TMP/audit.tsv"
say "→ le modèle choisit une action (peut prendre 10-30 s)…"
LLM_LOCAL_DIR="$MODEL_DIR" timeout 120 "$HERE/monitor_ai.py" --offline \
    --checks "$TMP/checks.tsv" --hardware "$TMP/hw.tsv" --audit "$TMP/audit.tsv" \
    || say "→ test non concluant (le monitor le signalera dans ai.log)"

say ""
say "Le monitor utilisera ce modèle UNIQUEMENT quand le réseau sera tombé."
say "Vérifier :"
say "  $HERE/install-local-llm.sh --check"
say "  $HERE/monitor.sh report | grep llm:local"
