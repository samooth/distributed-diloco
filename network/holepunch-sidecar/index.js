const fs = require('fs')
const path = require('path')
const net = require('net')
const crypto = require('crypto')
const Hypercore = require('hypercore')
const Hyperswarm = require('hyperswarm')
const Hyperdrive = require('hyperdrive')
const Corestore = require('corestore')
const HyperDB = require('hyperdb')
const dbDef = require('./spec/hyperdb')

const identity = require('./identity')

let _BitBeam, _DHT, _b4a, _b32, _bsv
function beamDeps() {
  if (!_BitBeam) {
    _BitBeam = require('bitbeam')
    _DHT = require('@hyperswarm/dht')
    _b4a = require('b4a')
    _b32 = require('hi-base32')
    _bsv = require('@bsv/sdk')
  }
  return { BitBeam: _BitBeam, DHT: _DHT, b4a: _b4a, b32: _b32, bsv: _bsv }
}

function arg(name, fallback = null) {
  const index = process.argv.indexOf(name)
  if (index === -1) return fallback
  return process.argv[index + 1]
}

function args(name) {
  const out = []
  for (let i = 0; i < process.argv.length; i++) {
    if (process.argv[i] === name && i + 1 < process.argv.length) out.push(process.argv[i + 1])
  }
  return out
}

function topicBuffer(topic) {
  return crypto.createHash('sha256').update(String(topic)).digest()
}

function topicToBeamKey(topic) {
  // Deterministic passphrase compatible with bitbeam v4 (base32, 32 bytes)
  const { BitBeam } = beamDeps()
  return BitBeam.toBase32(topicBuffer(topic))
}

function loadIdentityFile(file) {
  return identity.readIdentity(file)
}

function peerBeamKey(fromWif, toPubkey, context) {
  // Same derivation as BitBeam({from,to},{context}) in bitbeam v4
  const { b4a, b32, bsv } = beamDeps()
  const { PrivateKey, PublicKey, Hash } = bsv
  const from = PrivateKey.fromWif(fromWif)
  const to = PublicKey.fromString(toPubkey)
  const sharedPoint = from.deriveSharedSecret(to)
  const sharedSecret = b4a.from(sharedPoint.encode(true).slice(1))
  const ctx = b4a.from(context || 'bitbeam')
  const dhtKey = b4a.from(Hash.sha256(b4a.concat([sharedSecret, ctx])))
  return b32.encode(dhtKey).replace(/=/g, '').toLowerCase()
}

function readPeersFromFiles(files) {
  let peers = []
  for (const f of (files || [])) {
    try {
      const text = fs.readFileSync(f, 'utf8').trim()
      if (!text) continue
      try {
        const data = JSON.parse(text)
        if (Array.isArray(data)) peers.push(...data.map(String))
        else if (data && data.pubkey) peers.push(String(data.pubkey))
        else peers.push(...text.split(/\r?\n/).map(s => s.trim()).filter(Boolean))
      } catch {
        peers.push(...text.split(/\r?\n/).map(s => s.trim()).filter(s => s && !s.startsWith('#')))
      }
    } catch (e) {
      console.error('cannot read peer pubkey file:', f, e.message)
    }
  }
  return peers
}

function collectPeerPubkeys() {
  let peers = args('--peer-pubkey').filter(Boolean)
  peers.push(...readPeersFromFiles(args('--peer-pubkey-file')))
  // de-dup
  return [...new Set(peers.map(s => String(s).trim()).filter(Boolean))]
}

// Minimal multiplexing over a duplex stream (bitbeam/DHT connection).
// Frame: [streamId:4 BE][flags:1][len:4 BE][payload:len]
// flags: 0=data, 1=open (no payload required), 2=end, 3=destroy
const MUX_DATA = 0, MUX_OPEN = 1, MUX_END = 2, MUX_DESTROY = 3

function createMux(stream, onOpen) {
  const { b4a } = beamDeps()
  const streams = new Map()
  let buf = b4a.alloc(0)
  let nextId = 1

  function send(id, flags, payload) {
    const body = payload ? b4a.from(payload) : b4a.alloc(0)
    const head = b4a.alloc(9)
    head.writeUInt32BE(id >>> 0, 0)
    head[4] = flags
    head.writeUInt32BE(body.length >>> 0, 5)
    try { stream.write(b4a.concat([head, body])) } catch {}
  }

  stream.on('data', (chunk) => {
    buf = buf.length ? b4a.concat([buf, b4a.from(chunk)]) : b4a.from(chunk)
    while (buf.length >= 9) {
      const id = buf.readUInt32BE(0)
      const flags = buf[4]
      const len = buf.readUInt32BE(5)
      if (buf.length < 9 + len) break
      const payload = buf.subarray(9, 9 + len)
      buf = buf.subarray(9 + len)
      if (flags === MUX_OPEN) {
        if (onOpen) onOpen(id, payload)
      } else if (flags === MUX_DATA) {
        const s = streams.get(id)
        if (s) try { s.write(b4a.from(payload)) } catch {}
      } else if (flags === MUX_END) {
        const s = streams.get(id)
        if (s) try { s.end() } catch {}
      } else if (flags === MUX_DESTROY) {
        const s = streams.get(id)
        if (s) { streams.delete(id); try { s.destroy() } catch {} }
      }
    }
  })

  stream.on('error', () => { for (const s of streams.values()) try { s.destroy() } catch {}; streams.clear() })
  stream.on('close', () => { for (const s of streams.values()) try { s.destroy() } catch {}; streams.clear() })

  function openStream(localSocket) {
    const id = nextId++
    streams.set(id, localSocket)
    send(id, MUX_OPEN)
    localSocket.on('data', (d) => send(id, MUX_DATA, d))
    localSocket.on('end', () => send(id, MUX_END))
    localSocket.on('error', () => send(id, MUX_DESTROY))
    localSocket.on('close', () => { streams.delete(id); send(id, MUX_DESTROY) })
  }

  function bindRemote(id, localSocket) {
    streams.set(id, localSocket)
    localSocket.on('data', (d) => send(id, MUX_DATA, d))
    localSocket.on('end', () => send(id, MUX_END))
    localSocket.on('error', () => send(id, MUX_DESTROY))
    localSocket.on('close', () => { streams.delete(id); send(id, MUX_DESTROY) })
  }

  return { send, openStream, bindRemote, streams }
}

async function announce() {
  const topic = arg('--topic', 'diloco')
  const name = arg('--name', 'node')
  const runId = arg('--run-id', '')
  const endpoint = arg('--endpoint', '')
  const swarm = new Hyperswarm()
  const discovery = swarm.join(topicBuffer(topic), { server: true, client: true })
  swarm.on('connection', (conn, info) => {
    console.log(JSON.stringify({ event: 'peer', publicKey: info.publicKey.toString('hex'), client: info.client }))
    conn.write(JSON.stringify({ type: 'hello', name, runId, endpoint }) + '\n')
    conn.on('data', (data) => console.log(JSON.stringify({ event: 'message', data: data.toString().trim() })))
  })
  await discovery.flushed()
  console.log(JSON.stringify({ event: 'ready', topic, name, runId, endpoint, peers: swarm.connections.size }))
  await new Promise(() => {})
}

async function ledger() {
  const storage = arg('--storage', './ledger')
  const message = arg('--message')
  const core = new Hypercore(storage)
  await core.ready()
  if (message !== null) {
    await core.append(JSON.stringify({ time: Date.now(), message }))
    console.log(JSON.stringify({ event: 'appended', length: core.length }))
    await core.close()
    return
  }
  const blocks = []
  for await (const block of core.createReadStream()) {
    blocks.push(JSON.parse(block.toString()))
  }
  console.log(JSON.stringify({ event: 'ledger', blocks }, null, 2))
  await core.close()
}

async function walk(directory) {
  const result = []
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const full = path.join(directory, entry.name)
    if (entry.isDirectory()) {
      result.push(...(await walk(full)).map((item) => path.join(entry.name, item)))
    } else {
      result.push(entry.name)
    }
  }
  return result
}

async function share() {
  const storage = arg('--storage', './drive')
  const directory = arg('--dir')
  if (!directory) throw new Error('--dir is required')
  const store = new (require('corestore'))(storage)
  const drive = new Hyperdrive(store)
  await drive.ready()
  const files = await walk(directory)
  for (const relative of files) {
    const full = path.join(directory, relative)
    await drive.put('/' + relative.replace(/\\/g, '/'), fs.readFileSync(full))
  }
  console.log(JSON.stringify({ event: 'shared', key: drive.key.toString('hex'), discoveryKey: drive.discoveryKey.toString('hex'), files: files.length }))
  const topic = arg('--topic', 'diloco')
  const swarm = new Hyperswarm()
  const discovery = swarm.join(topicBuffer(topic), { server: true, client: true })
  swarm.on('connection', (conn, info) => {
    const stream = drive.replicate(info.client)
    conn.pipe(stream).pipe(conn)
  })
  await Promise.race([discovery.flushed(), new Promise((resolve) => setTimeout(resolve, 1000))])
  console.log(JSON.stringify({ event: 'announced', topic }))
  await new Promise(() => {})
}

async function index() {
  const storage = arg('--storage', './index')
  const key = arg('--key')
  const value = arg('--value')
  const core = new Hypercore(storage)
  await core.ready()
  const db = HyperDB.bee(core, dbDef)
  await db.ready()
  if (key !== null) {
    await db.insert('@diloco/metadata', { key, value: value || '', updated_at: Date.now() })
    await db.flush()
    console.log(JSON.stringify({ event: 'indexed', key }))
  } else {
    const rows = await db.find('@diloco/metadata', {}).toArray()
    console.log(JSON.stringify({ event: 'index', rows }, null, 2))
  }
  await db.close()
  await core.close()
}

async function tunnelServer() {
  const { BitBeam, DHT } = beamDeps()
  const topic = arg('--topic', 'diloco')
  const targetHost = arg('--target-host', '127.0.0.1')
  const targetPort = Number(arg('--target-port', '0'))
  const identityFile = arg('--identity')
  const context = arg('--context', topic)
  if (!targetPort) throw new Error('--target-port is required')

  // Identity mode: listen for per-pair ECDH beams from known peer pubkeys
  if (identityFile) {
    const id = loadIdentityFile(identityFile)
    if (!id.wif) throw new Error('identity file must include wif for tunnel server')
    const peerFiles = args('--peer-pubkey-file')
    let peers = collectPeerPubkeys()
    if (!peers.length && peerFiles.length === 0) {
      throw new Error('identity tunnel-server requires --peer-pubkey or --peer-pubkey-file')
    }

    const node = new DHT({ ephemeral: true })
    await node.ready()
    const listeners = new Map() // peerPub -> { key, server }

    async function addPeerListener(peerPub) {
      const p = String(peerPub || '').trim()
      if (!p || listeners.has(p)) return false
      try {
        const key = peerBeamKey(id.wif, p, context)
        const keyPair = DHT.keyPair(BitBeam.fromBase32(key))
        const server = node.createServer({
          firewall (remotePublicKey) {
            return !remotePublicKey.equals(keyPair.publicKey)
          }
        })
        server.on('connection', (conn) => {
          let mux
          mux = createMux(conn, (sid) => {
            const target = net.connect(targetPort, targetHost)
            try { target.setNoDelay(true) } catch {}
            mux.bindRemote(sid, target)
            target.on('error', (e) => console.error('tunnel-server-id target error', e.message))
          })
          console.log(JSON.stringify({ event: 'peer-connected', mode: 'tunnel-server-id', peer: p }))
          conn.on('error', (e) => console.error('tunnel-server-id peer error', e.message))
          conn.on('close', () => console.log(JSON.stringify({ event: 'peer-disconnected', mode: 'tunnel-server-id', peer: p })))
        })
        await server.listen(keyPair)
        listeners.set(p, { key, server })
        console.log(JSON.stringify({ event: 'peer-listener-added', mode: 'tunnel-server-id', peer: p }))
        return true
      } catch (e) {
        console.error('tunnel-server-id failed to add peer listener', p, e && e.message ? e.message : e)
        return false
      }
    }

    for (const peerPub of peers) {
      await addPeerListener(peerPub)
    }

    // If started with only peer files (allowlist not known yet), wait and allow dynamic late join
    if (listeners.size === 0 && peerFiles.length > 0) {
      console.log(JSON.stringify({ event: 'waiting-peers', mode: 'tunnel-server-id', topic, context, transport: 'bitbeam-identity', target: `${targetHost}:${targetPort}`, coordinator_pubkey: id.pubkey }))
      // poll until at least one peer appears
      while (listeners.size === 0) {
        await new Promise((r) => setTimeout(r, 2000))
        const dynamic = readPeersFromFiles(peerFiles)
        for (const p of dynamic) await addPeerListener(p)
      }
    } else if (listeners.size === 0) {
      throw new Error('identity tunnel-server requires at least one valid peer pubkey')
    }

    console.log(JSON.stringify({ event: 'ready', mode: 'tunnel-server-id', topic, context, transport: 'bitbeam-identity', target: `${targetHost}:${targetPort}`, peers: listeners.size, coordinator_pubkey: id.pubkey }))

    // Dynamic late-join: re-read peer pubkey files and add listeners for new peers
    if (peerFiles.length > 0) {
      (async () => {
        while (true) {
          await new Promise((r) => setTimeout(r, 5000))
          try {
            const dynamic = readPeersFromFiles(peerFiles)
            let added = 0
            for (const p of dynamic) {
              if (!listeners.has(String(p || '').trim())) {
                const ok = await addPeerListener(p)
                if (ok) added++
              }
            }
            if (added > 0) {
              console.log(JSON.stringify({ event: 'peers-updated', mode: 'tunnel-server-id', peers: listeners.size, added }))
            }
          } catch (e) {
            console.error('tunnel-server-id peer refresh error', e && e.message ? e.message : e)
          }
        }
      })().catch((e) => console.error('tunnel-server-id watcher fatal', e && e.message ? e.message : e))
    }

    await new Promise(() => {})
    return
  }

  // Topic/passphrase mode (legacy/shared)
  const key = topicToBeamKey(topic)
  const keyPair = DHT.keyPair(BitBeam.fromBase32(key))
  const node = new DHT({ ephemeral: true })
  await node.ready()
  const server = node.createServer({
    firewall (remotePublicKey) {
      return !remotePublicKey.equals(keyPair.publicKey)
    }
  })

  server.on('connection', (conn) => {
    let mux
    mux = createMux(conn, (id) => {
      const target = net.connect(targetPort, targetHost)
      try { target.setNoDelay(true) } catch {}
      mux.bindRemote(id, target)
      target.on('error', (e) => console.error('tunnel-server target error', e.message))
    })
    conn.on('error', (e) => console.error('tunnel-server peer error', e.message))
  })

  await server.listen(keyPair)
  console.log(JSON.stringify({ event: 'ready', mode: 'tunnel-server', topic, key, transport: 'bitbeam', target: `${targetHost}:${targetPort}` }))
  await new Promise(() => {})
}

async function tunnelClient() {
  const { BitBeam } = beamDeps()
  const topic = arg('--topic', 'diloco')
  const listenHost = arg('--listen-host', '127.0.0.1')
  const listenPort = Number(arg('--listen-port', '0'))
  const identityFile = arg('--identity')
  const peerPubkey = arg('--peer-pubkey')
  const context = arg('--context', topic)
  if (!listenPort) throw new Error('--listen-port is required')

  let mode = 'tunnel-client'
  let transport = 'bitbeam'
  let info = { topic }
  let ident = null

  if (identityFile || peerPubkey) {
    if (!identityFile) throw new Error('--identity required for identity tunnel-client')
    if (!peerPubkey) throw new Error('--peer-pubkey (coordinator pubkey) required for identity tunnel-client')
    ident = loadIdentityFile(identityFile)
    if (!ident.wif) throw new Error('identity file must include wif for tunnel client')
    mode = 'tunnel-client-id'
    transport = 'bitbeam-identity'
    info = { topic, context, coordinator_pubkey: peerPubkey, node_pubkey: ident.pubkey }
  } else {
    const key = topicToBeamKey(topic)
    info = { topic, key }
  }

  let currentMux = null
  let currentBeam = null
  let readyEmitted = false
  let stopping = false

  const server = net.createServer((socket) => {
    try { socket.setNoDelay(true) } catch {}
    if (currentMux) {
      try { currentMux.openStream(socket); return } catch (e) {
        console.error(`${mode} mux open failed:`, e.message)
      }
    }
    // No active tunnel - fail fast so caller can retry
    try { socket.destroy() } catch {}
  })

  await new Promise((resolve, reject) => {
    server.listen(listenPort, listenHost, () => {
      console.log(JSON.stringify({ event: 'listening', mode, ...info, transport, listen: `${listenHost}:${listenPort}` }))
      resolve()
    })
    server.on('error', reject)
  })

  function createBeam() {
    if (identityFile || peerPubkey) {
      const { PrivateKey, PublicKey } = beamDeps().bsv
      const from = PrivateKey.fromWif(ident.wif)
      const to = PublicKey.fromString(peerPubkey)
      return new BitBeam({ from, to }, { announce: false, context })
    } else {
      const key = topicToBeamKey(topic)
      return new BitBeam(key, { announce: false })
    }
  }

  async function connectOnce() {
    const beam = createBeam()
    currentBeam = beam
    await new Promise((resolve, reject) => {
      let settled = false
      const onErr = (e) => { if (settled) return; settled = true; cleanup(); reject(e) }
      const onConn = () => { if (settled) return; settled = true; cleanup(); resolve() }
      const onClose = () => { if (settled) return; settled = true; cleanup(); reject(new Error('beam closed before connect')) }
      const cleanup = () => {
        beam.off('connected', onConn); beam.off('error', onErr); beam.off('close', onClose)
      }
      beam.once('connected', onConn)
      beam.once('error', onErr)
      beam.once('close', onClose)
      try { beam.resume() } catch (e) { onErr(e) }
    })
    const mux = createMux(beam, null)
    currentMux = mux
    console.log(JSON.stringify({ event: readyEmitted ? 'reconnected' : 'ready', mode, ...info, transport, listen: `${listenHost}:${listenPort}` }))
    readyEmitted = true

    const disconnected = new Promise((resolve) => {
      let done = false
      const finish = (reason) => {
        if (done) return
        done = true
        try { beam.removeListener('error', onError); beam.removeListener('close', onClose) } catch {}
        if (currentBeam === beam) currentBeam = null
        if (currentMux === mux) currentMux = null
        // destroy any active substreams on this mux
        try { for (const s of mux.streams.values()) { try { s.destroy() } catch {} } mux.streams.clear() } catch {}
        console.error(`${mode} beam disconnected: ${reason}`)
        resolve()
      }
      const onError = (e) => finish(e && e.message ? e.message : 'error')
      const onClose = () => finish('close')
      beam.on('error', onError)
      beam.on('close', onClose)
    })
    await disconnected
  }

  ;(async () => {
    let attempt = 0
    while (!stopping) {
      try {
        console.log(JSON.stringify({ event: attempt === 0 ? 'connecting' : 'reconnecting', mode, ...info, transport, attempt: attempt + 1 }))
        await connectOnce()
        attempt = 0 // reset backoff after a successful connection lifecycle
      } catch (e) {
        const msg = e && e.message ? e.message : String(e)
        console.error(`${mode} connect error: ${msg}`)
      }
      if (stopping) break
      attempt += 1
      const delay = Math.min(1000 * Math.pow(1.6, Math.min(attempt, 8)), 15000)
      await new Promise((r) => setTimeout(r, delay))
    }
  })().catch((e) => {
    console.error(`${mode} fatal:`, e && e.message ? e.message : e)
  })

  await new Promise(() => {})
}

const mode = arg('--mode', 'announce')
if (mode === 'announce') announce().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'ledger') ledger().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'share') share().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'index') index().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'tunnel-server' || mode === 'tunnel-server-id') tunnelServer().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'tunnel-client' || mode === 'tunnel-client-id') tunnelClient().catch((error) => { console.error(error); process.exit(1) })
else { console.error(`unknown mode: ${mode}`); process.exit(2) }
