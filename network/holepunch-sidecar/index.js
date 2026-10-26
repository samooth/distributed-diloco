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

function arg(name, fallback = null) {
  const index = process.argv.indexOf(name)
  if (index === -1) return fallback
  return process.argv[index + 1]
}

function topicBuffer(topic) {
  return crypto.createHash('sha256').update(String(topic)).digest()
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
  const topic = arg('--topic', 'diloco')
  const targetHost = arg('--target-host', '127.0.0.1')
  const targetPort = Number(arg('--target-port', '0'))
  if (!targetPort) throw new Error('--target-port is required')
  const swarm = new Hyperswarm()
  const discovery = swarm.join(topicBuffer(topic), { server: true, client: false })
  swarm.on('connection', (conn, info) => {
    const target = net.connect(targetPort, targetHost)
    conn.pipe(target)
    target.pipe(conn)
    const close = () => { try { conn.destroy() } catch {} ; try { target.destroy() } catch {} }
    conn.on('close', close)
    target.on('close', close)
    conn.on('error', close)
    target.on('error', close)
  })
  await discovery.flushed()
  console.log(JSON.stringify({ event: 'ready', mode: 'tunnel-server', topic, target: `${targetHost}:${targetPort}` }))
  await new Promise(() => {})
}

async function tunnelClient() {
  const topic = arg('--topic', 'diloco')
  const listenHost = arg('--listen-host', '127.0.0.1')
  const listenPort = Number(arg('--listen-port', '0'))
  if (!listenPort) throw new Error('--listen-port is required')
  const server = net.createServer((socket) => {
    const swarm = new Hyperswarm()
    const discovery = swarm.join(topicBuffer(topic), { server: false, client: true })
    let connected = false
    const finish = () => {
      try { socket.destroy() } catch {}
      try { swarm.destroy() } catch {}
    }
    const timer = setTimeout(() => {
      if (!connected) {
        console.log(JSON.stringify({ event: 'tunnel-error', message: 'no peer connected' }))
        finish()
      }
    }, 10000)
    swarm.on('connection', (conn, info) => {
      if (connected) return
      connected = true
      clearTimeout(timer)
      socket.pipe(conn)
      conn.pipe(socket)
      const close = () => { try { socket.destroy() } catch {} ; try { conn.destroy() } catch {} ; try { swarm.destroy() } catch {} }
      socket.on('close', close)
      conn.on('close', close)
      socket.on('error', close)
      conn.on('error', close)
    })
    discovery.flushed().then(() => {
      if (!connected) {
        console.log(JSON.stringify({ event: 'waiting-peer', topic }))
      }
    }).catch((error) => {
      console.log(JSON.stringify({ event: 'tunnel-error', message: String(error) }))
      finish()
    })
  })
  await new Promise((resolve, reject) => {
    server.listen(listenPort, listenHost, () => {
      console.log(JSON.stringify({ event: 'ready', mode: 'tunnel-client', topic, listen: `${listenHost}:${listenPort}` }))
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
