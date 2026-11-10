#!/usr/bin/env node
const fs = require('fs')
const path = require('path')
const { PrivateKey, PublicKey, Hash, Signature, BSM } = require('@bsv/sdk')

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name)
  if (i === -1) return fallback
  return process.argv[i + 1]
}

function readIdentity(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'))
}

function msgBytes(message) {
  return Buffer.isBuffer(message) ? Array.from(message) : Array.from(Buffer.from(String(message), 'utf8'))
}

function sha256Hex(message) {
  return Buffer.from(Hash.sha256(msgBytes(message))).toString('hex')
}

function signMessage(priv, message) {
  const hash = Hash.sha256(msgBytes(message))
  const sig = priv.sign(hash)
  const der = Buffer.from(sig.toDER()).toString('hex')
  return { sig: der, digest: Buffer.from(hash).toString('hex'), alg: 'ecdsa-sha256-der' }
}

function verifyMessage(pubkey, message, sigHex) {
  try {
    const hash = Hash.sha256(msgBytes(message))
    const pub = PublicKey.fromString(pubkey)
    const sig = Signature.fromDER(sigHex, 'hex')
    return !!pub.verify(hash, sig)
  } catch (e) {
    return false
  }
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
  const data = readIdentity(file)
  console.log(JSON.stringify({ pubkey: data.pubkey, name: data.name || null }))
}

function cmdSign() {
  const file = arg('--file')
  const message = arg('--message')
  if (!file) throw new Error('--file required')
  if (message == null) throw new Error('--message required')
  const data = readIdentity(file)
  const priv = PrivateKey.fromWif(data.wif)
  const out = signMessage(priv, message)
  console.log(JSON.stringify({ pubkey: data.pubkey, ...out }))
}

function cmdVerify() {
  const message = arg('--message')
  const sig = arg('--sig')
  const pubkey = arg('--pubkey')
  if (message == null) throw new Error('--message required')
  if (!sig) throw new Error('--sig required')
  if (!pubkey) throw new Error('--pubkey required')
  const ok = verifyMessage(pubkey, message, sig)
  console.log(JSON.stringify({ ok, pubkey, digest: sha256Hex(message) }))
}

function main() {
  const cmd = arg('--cmd', 'generate')
  if (cmd === 'generate') cmdGenerate()
  else if (cmd === 'pubkey') cmdPubkey()
  else if (cmd === 'sign') cmdSign()
  else if (cmd === 'verify') cmdVerify()
  else { console.error('unknown cmd: ' + cmd); process.exit(2) }
}

if (require.main === module) {
  try { main() } catch (e) { console.error(e && e.message ? e.message : e); process.exit(1) }
}

module.exports = { readIdentity, sha256Hex, signMessage, verifyMessage }
