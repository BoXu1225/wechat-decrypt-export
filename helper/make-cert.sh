#!/bin/bash
# Create a local self-signed code-signing identity for WeChatSendHelper.app.
#
# Why: macOS ties Accessibility / Screen Recording grants to the app's code
# signature. An ad-hoc signature changes with every build, so each rebuild
# silently drops the grants. With a stable certificate, the designated
# requirement is "identifier + this certificate", which survives rebuilds.
#
# The private key stays in your login keychain (only /usr/bin/codesign may use
# it without asking). Nothing is trusted system-wide. Remove with:
#   security delete-identity -c "WeChatSendHelper Local Signing"
set -euo pipefail

NAME="WeChatSendHelper Local Signing"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-certificate -c "$NAME" "$KEYCHAIN" >/dev/null 2>&1; then
    echo "[+] signing identity '$NAME' already exists"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
umask 077
cat > "$TMP/cert.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
CNF
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/cert.cnf" 2>/dev/null
PASS="$(openssl rand -hex 16)"
openssl pkcs12 -export -legacy -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
    -name "$NAME" -passout "pass:$PASS" -out "$TMP/id.p12" 2>/dev/null \
  || openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
    -name "$NAME" -passout "pass:$PASS" -out "$TMP/id.p12"
security import "$TMP/id.p12" -k "$KEYCHAIN" -P "$PASS" -T /usr/bin/codesign >/dev/null
echo "[+] created signing identity '$NAME' in the login keychain"

# Let Apple's signing tools use the key without a keychain prompt on every
# build (only this key; asks for the login password once).
if [[ -t 0 ]]; then
    echo "[i] macOS needs your login password once so builds don't prompt each time:"
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -l "$NAME" "$KEYCHAIN" >/dev/null \
        || echo "[i] skipped; each build may ask for the keychain password"
else
    echo "[i] to stop keychain prompts on every build, run once in a terminal:"
    echo "    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -l \"$NAME\" $KEYCHAIN"
fi
