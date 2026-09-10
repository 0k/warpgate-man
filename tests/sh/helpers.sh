# shellcheck shell=bash
# Shared fixtures for the odoo-get-ssh-config tests.
#
# The script's job is to bootstrap tools and then delegate, so almost every
# test needs a sandbox where `uv`, `uvx`, `git` and the package manager are
# fakes. Real ones would install software and hit the network.
#
# This file is SOURCED by sunit test files, never executed: `$base` comes
# from bin/test-sh and `$test_tmpdir` from sunit itself, so shellcheck
# cannot see where they are assigned.
# shellcheck disable=SC2154  # base/test_tmpdir are provided by the harness

# shellcheck disable=SC2034  # consumed by the test files that source this
script_under_test="$base/scripts/odoo-get-ssh-config"

# A PATH holding ONLY the sandbox.
#
# Tests that hide a tool must not merely delete the fake. Prepending the
# sandbox to the caller's PATH leaves the developer's real git/uv visible
# behind it, and including /usr/bin does the same — git lives there on
# most machines. Either way a "missing tool" scenario would silently
# exercise the present-tool path and pass for the wrong reason.
#
# So the sandbox is the WHOLE PATH, and the coreutils the script genuinely
# uses are symlinked into it by ogsc_init.
ogsc_path() {
    echo "$test_tmpdir/sandbox/bin"
}

# The commands the script legitimately calls, beyond the ones under test.
# Symlinked rather than left to /usr/bin so the PATH can stay closed.
OGSC_COREUTILS=(
    basename dirname id mkdir cat sed grep stty printf
    tr cut readlink realpath uname date rm
    # used by the tests themselves, not by the script
    script stat md5sum diff env setsid bash sh chmod ln
)

# Build a sandbox in $test_tmpdir with a fake PATH.
#
# The fake uvx prints a fixed ssh config to stdout (or to --output) plus
# noise on stderr, mirroring the real one: that is what lets the tests
# assert the script keeps stdout clean.
ogsc_init() {
    cd "$test_tmpdir" || return 1
    rm -rf sandbox
    mkdir -p sandbox/bin sandbox/home

    local tool real
    for tool in "${OGSC_COREUTILS[@]}"; do
        real="$(command -v "$tool" 2>/dev/null)" || continue
        ln -sf "$real" "$test_tmpdir/sandbox/bin/$tool"
    done

    ogsc_fake_uv
    ogsc_fake_uvx
    ogsc_fake_tool git
}

ogsc_fake_uv() {
    cat > "$test_tmpdir/sandbox/bin/uv" <<'EOF'
#!/bin/bash
echo "uv $*" >> "$OGSC_CALLS"
EOF
    chmod +x "$test_tmpdir/sandbox/bin/uv"
}

ogsc_fake_uvx() {
    cat > "$test_tmpdir/sandbox/bin/uvx" <<'EOF'
#!/bin/bash
echo "uvx $*" >> "$OGSC_CALLS"
echo "Resolved 26 packages" >&2   # the real uvx is noisy on stderr
outfile="" prev=""
for a in "$@"; do
    [ "$prev" = "--output" ] && outfile="$a"
    prev="$a"
done
config="Host jev-prod
    HostName warpgate.example.com
    Port     2222
    User     you@example.com:jev-prod"
if [ -n "$outfile" ]; then
    printf '%s\n' "$config" > "$outfile"
else
    printf '%s\n' "$config"
fi
EOF
    chmod +x "$test_tmpdir/sandbox/bin/uvx"
}

# A fake tool that merely records that it was called.
ogsc_fake_tool() {
    local name="$1"
    cat > "$test_tmpdir/sandbox/bin/$name" <<EOF
#!/bin/bash
echo "$name \$*" >> "\$OGSC_CALLS"
exit 0
EOF
    chmod +x "$test_tmpdir/sandbox/bin/$name"
}

ogsc_remove_tool() {
    rm -f "$test_tmpdir/sandbox/bin/$1"
}

# A fake apt-get that RECORDS the install and then materialises the tool,
# so the script's post-install `command -v` check succeeds.
#
# Never installs anything for real: the suite must not need root, must not
# touch the network, and must not mutate the machine running it.
ogsc_fake_package_manager() {
    cat > "$test_tmpdir/sandbox/bin/apt-get" <<EOF
#!/bin/bash
echo "apt-get \$*" >> "\$OGSC_CALLS"
for arg in "\$@"; do
    case "\$arg" in
        -*|update|install) continue ;;
        *)
            printf '#!/bin/bash\necho "%s \$*" >> "\$OGSC_CALLS"\n' "\$arg" \
                > "$test_tmpdir/sandbox/bin/\$arg"
            chmod +x "$test_tmpdir/sandbox/bin/\$arg"
            ;;
    esac
done
exit 0
EOF
    chmod +x "$test_tmpdir/sandbox/bin/apt-get"

    # Installing as root avoids needing a fake sudo in the closed PATH.
    #
    # ogsc_init symlinked the real `id` here; writing through the symlink
    # would try to modify /usr/bin/id, so replace the link itself.
    rm -f "$test_tmpdir/sandbox/bin/id"
    cat > "$test_tmpdir/sandbox/bin/id" <<'EOF'
#!/bin/bash
[ "$1" = "-u" ] && { echo 0; exit 0; }
exec /usr/bin/id "$@"
EOF
    chmod +x "$test_tmpdir/sandbox/bin/id"
}

# A ready-made config, so tests that are not about setup skip the prompts.
ogsc_write_config() {
    cat > "$test_tmpdir/sandbox/config.yaml" <<'EOF'
servers:
  - name: bastion
    url: https://warpgate.example.com
    ssh-host: warpgate.example.com
    ssh-port: 2222

odoo:
  url: https://odoo.example.com
  db: mydb
  user: you@example.com
  password: ${ODOO_PASSWORD}
EOF
}
