#!/usr/bin/env node
const fs = require('fs')
const path = require('path')
const { PrivateKey } = require('@bsv/sdk')

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name)
  if (i === -1) return fallback
  return process.argv[i + 1]
}

function cmdGenerate() {
  const out = arg('--out')
  const name = arg('--name', 'node')
  const priv = PrivateKey.fromRandom()
  const wif = priv.toWif()
  const pub = priv.toPublicKey().toString()
  const data = { name, pubkey: pub, wif, created_at: new Date().toISOString(), format: 'bsv-sdk-v1' }
  if (out) {
    const dir = path.dirname(out)
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(out, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 })
    try { fs.chmodSync(out, 0o600) } catch {}
  }
  console.log(JSON.stringify({ pubkey: pub, out: out || null, name }))
}

function cmdPubkey() {
  const file = arg('--file')
  if (!file) throw new Error('--file required')
  const data = JSON.parse(fs.readFileSync(file, 'utf8'))
  console.log(JSON.stringify({ pubkey: data.pubkey, name: data.name || null }))
}

const cmd = arg('--cmd', 'generate')
if (cmd === 'generate') cmdGenerate()
else if (cmd === 'pubkey') cmdPubkey()
else { console.error('unknown cmd: ' + cmd); process.exit(2) }
