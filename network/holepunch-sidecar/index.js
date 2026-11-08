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

let _BitBeam, _DHT, _b4a, _b32
function beamDeps() {
  if (!_BitBeam) {
    _BitBeam = require('bitbeam')
    _DHT = require('@hyperswarm/dht')
    _b4a = require('b4a')
    _b32 = require('hi-base32')
  }
  return { BitBeam: _BitBeam, DHT: _DHT, b4a: _b4a, b32: _b32 }
}

function arg(name, fallback = null) {
  const index = process.argv.indexOf(name)
  if (index === -1) return fallback
  return process.argv[index + 1]
}

function topicBuffer(topic) {
  return crypto.createHash('sha256').update(String(topic)).digest()
}

function topicToBeamKey(topic) {
  // Deterministic passphrase compatible with bitbeam v4 (base32, 32 bytes)
  const { BitBeam } = beamDeps()
  return BitBeam.toBase32(topicBuffer(topic))
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
  if (!targetPort) throw new Error('--target-port is required')

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
  if (!listenPort) throw new Error('--listen-port is required')

  const key = topicToBeamKey(topic)
  console.log(JSON.stringify({ event: 'connecting', mode: 'tunnel-client', topic, key, transport: 'bitbeam' }))

  const beam = new BitBeam(key, { announce: false })
  await new Promise((resolve, reject) => {
    const onErr = (e) => { cleanup(); reject(e) }
    const onConn = () => { cleanup(); resolve() }
    const cleanup = () => { beam.off('connected', onConn); beam.off('error', onErr) }
    beam.once('connected', onConn)
    beam.once('error', onErr)
    beam.resume() // trigger _open in streamx/duplex implementations
  })

  const mux = createMux(beam, null)
  beam.on('error', (e) => console.error('tunnel-client beam error', e.message))
  beam.on('close', () => console.error('tunnel-client beam closed'))

  const server = net.createServer((socket) => {
    try { socket.setNoDelay(true) } catch {}
    mux.openStream(socket)
  })
  await new Promise((resolve, reject) => {
    server.listen(listenPort, listenHost, () => {
      console.log(JSON.stringify({ event: 'ready', mode: 'tunnel-client', topic, key, transport: 'bitbeam', listen: `${listenHost}:${listenPort}` }))
      resolve()
    })
    server.on('error', reject)
  })
  await new Promise(() => {})
}

const mode = arg('--mode', 'announce')
if (mode === 'announce') announce().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'ledger') ledger().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'share') share().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'index') index().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'tunnel-server') tunnelServer().catch((error) => { console.error(error); process.exit(1) })
else if (mode === 'tunnel-client') tunnelClient().catch((error) => { console.error(error); process.exit(1) })
else { console.error(`unknown mode: ${mode}`); process.exit(2) }
