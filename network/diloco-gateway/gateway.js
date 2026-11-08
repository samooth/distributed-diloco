import http from 'node:http'
import { WebSocketServer } from 'ws'
let Hyperswarm = null
try { Hyperswarm = (await import('hyperswarm')).default } catch (e) { console.warn('Hyperswarm unavailable:', e.message) }
import crypto from 'node:crypto'

const PORT = process.env.PORT || 8080
const DEBUG = process.env.DEBUG === '1'
function dlog(...args) { if (DEBUG) console.log('[DEBUG]', ...args) }
const MNEE_API_KEY = process.env.MNEE_API_KEY || ''
const MNEE_BASE = process.env.MNEE_ENV === 'production'
  ? 'https://proxy-api.mnee.net'
  : 'https://sandbox-proxy-api.mnee.net'

const server = http.createServer(async (req, res) => {
  const allowedOrigins = (process.env.ALLOWED_ORIGINS || 'http://localhost,https://diloco.local').split(',')
  const origin = req.headers.origin
  if (allowedOrigins.includes(origin)) res.setHeader('Access-Control-Allow-Origin', origin)
  else if (allowedOrigins.includes('*')) res.setHeader('Access-Control-Allow-Origin', '*')
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type')
  if (req.method === 'OPTIONS') { res.statusCode = 204; res.end(); return }
  if (req.url === '/health') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ ok: true })); return }

  // WOC tx hex proxy (CORS)
  const wocHexMatch = req.url.match(/^\/woc\/tx\/([0-9a-f]{64})\/hex$/i)
  if (wocHexMatch && req.method === 'GET') {
    const txid = wocHexMatch[1]
    try {
      const r = await fetch('https://api.whatsonchain.com/v1/bsv/main/tx/' + txid + '/hex')
      const text = await r.text()
      res.statusCode = r.status
      res.setHeader('Content-Type', 'text/plain')
      res.setHeader('Access-Control-Allow-Origin', '*')
      res.end(text)
    } catch (e) {
      res.statusCode = 500; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ error: e.message }))
    }
    return
  }
  if (req.url === '/mnee/balance' && req.method === 'POST') {
    let body = ''
    req.on('data', (c) => (body += c))
    req.on('end', async () => {
      try {
        const addresses = JSON.parse(body)
        if (!Array.isArray(addresses)) throw new Error('addresses must be array')
        if (!MNEE_API_KEY) { res.statusCode = 503; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ error: 'MNEE_API_KEY not configured' })); return }
        const r = await fetch(MNEE_BASE + '/v2/balance?auth_token=' + encodeURIComponent(MNEE_API_KEY), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(addresses) })
        const data = await r.text()
        res.statusCode = r.status; res.setHeader('Content-Type', 'application/json'); res.end(data)
      } catch (e) { res.statusCode = 400; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ error: e.message })) }
    })
    return
  }
  res.statusCode = 404; res.end('not found')
})

const wss = new WebSocketServer({ server, maxPayload: 1024 * 1024 })
const swarm = Hyperswarm ? new Hyperswarm() : null

// Room management
const rooms = new Map() // roomId -> { players: Map(playerId -> ws), hostId, state }
const playerIdentity = new Map() // playerId -> identityKey (hex)
const rateLimit = new Map() // playerId -> { tokens, last }
const topics = new Map()

function topicHash(roomId) { return crypto.createHash('sha256').update('diloco-room:' + roomId).digest() }
function joinRoom(roomId) {
  if (topics.has(roomId)) return topics.get(roomId)
  if (!swarm) return null
  const hash = topicHash(roomId)
  const discovery = swarm.join(hash, { server: true, client: true })
  discovery.flushed().then(() => console.log('swarm joined', roomId))
  topics.set(roomId, discovery)
  return discovery
}

function getRoom(roomId) {
  if (!rooms.has(roomId)) rooms.set(roomId, { players: new Map(), hostId: null, state: null })
  return rooms.get(roomId)
}

function broadcast(roomId, msg, excludeWs = null) {
  const room = rooms.get(roomId)
  if (!room) return
  const data = JSON.stringify(msg)
  for (const [pid, ws] of room.players) {
    if (ws !== excludeWs && ws.readyState === 1) ws.send(data)
  }
  // relay to swarm (if available)
  if (swarm) {
    const buf = Buffer.from(data)
    for (const conn of swarm.connections) { try { conn.write(buf) } catch (e) {} }
  }
}

if (swarm) {
  swarm.on('connection', (conn) => {
    conn.on('data', (data) => {
      try {
        const msg = JSON.parse(data.toString())
        const room = msg.room
        if (!room || !rooms.has(room)) return
        broadcast(room, msg)
      } catch (e) {}
    })
  })
}

wss.on('connection', (ws) => {
  let currentRoom = null
  let playerId = crypto.randomUUID()
  let playerName = 'Player'
  let identityKey = null
  dlog('new connection', playerId)

  ws.on('message', async (raw) => {
    let msg
    try { msg = JSON.parse(raw.toString()) } catch { return }
    if (msg.type === 'create-room') {
      dlog('create-room', msg.room, 'by', playerId)
      const roomId = msg.room || crypto.randomUUID().slice(0, 8)
      const inviteCode = crypto.randomBytes(8).toString('hex')
      const room = getRoom(roomId)
      room.inviteCode = inviteCode
      if (!room.hostId) room.hostId = playerId
      room.players.set(playerId, ws)
      currentRoom = roomId
      playerName = String(msg.name || 'Player').slice(0, 32)
      identityKey = msg.identityKey || null
      playerIdentity.set(playerId, identityKey)
      ws.send(JSON.stringify({ type: 'room-created', roomId, inviteCode, playerId }))
      return
    }
    if (msg.type === 'join') {
      dlog('join', msg.room, 'by', playerId, msg.name)
      currentRoom = msg.room
      playerName = String(msg.name || 'Player').slice(0, 32)
      identityKey = msg.identityKey || null
      const room = getRoom(currentRoom)
      if (msg.inviteCode && room.inviteCode && msg.inviteCode !== room.inviteCode) {
        ws.send(JSON.stringify({ type: 'error', message: 'Invalid invite code' })); return
      }
      playerIdentity.set(playerId, identityKey)
      if (!room.hostId) room.hostId = playerId
      for (const [pid, pws] of room.players) {
        if (pws.readyState === 1) pws.send(JSON.stringify({ type: 'peer-joined', room: currentRoom, playerId, name: playerName }))
      }
      room.players.set(playerId, ws)
      joinRoom(currentRoom)
      ws.send(JSON.stringify({ type: 'joined', room: currentRoom, playerId, hostId: room.hostId, isHost: room.hostId === playerId, peers: [...room.players.keys()].filter(p => p !== playerId) }))
      broadcast(currentRoom, { type: 'playerJoined', room: currentRoom, playerId, name: playerName }, ws)
      const playerList = [...room.players.keys()].map((pid) => ({ playerId: pid }))
      ws.send(JSON.stringify({ type: 'playerList', room: currentRoom, players: playerList, hostId: room.hostId }))
      return
    }
    if (!currentRoom) return
    const room = getRoom(currentRoom)
    // sanitize
    if (typeof msg.type !== 'string') return
    // Signaling messages (WebRTC)
    if (msg.type === 'offer' || msg.type === 'answer' || msg.type === 'ice') {
      const target = room.players.get(msg.target)
      if (target && target.readyState === 1) target.send(JSON.stringify({ ...msg, from: playerId }))
      return
    }
    if (msg.type === 'chat') {
      const text = String(msg.text || '').slice(0, 500)
      for (const [pid, pws] of room.players) {
        if (pid !== playerId && pws.readyState === 1) pws.send(JSON.stringify({ type: 'chat', from: playerId, name: playerName, text }))
      }
      return
    }
    if (msg.type === 'leave') {
      room.players.delete(playerId)
      for (const [pid, pws] of room.players) {
        if (pws.readyState === 1) pws.send(JSON.stringify({ type: 'peer-left', room: currentRoom, playerId }))
      }
      // migrate host
      if (room.hostId === playerId && room.players.size > 0) {
        const nextHost = room.players.keys().next().value
        room.hostId = nextHost
        broadcast(currentRoom, { type: 'hostChanged', room: currentRoom, hostId: nextHost })
        const hostWs = room.players.get(nextHost)
        if (hostWs && hostWs.readyState === 1) hostWs.send(JSON.stringify({ type: 'youAreHost', room: currentRoom }))
      }
      if (room.players.size === 0) rooms.delete(currentRoom)
      return
    }

    if (msg.type === 'input') {
      dlog('input', msg.action, 'from', playerId, 'room', currentRoom)
      // rate limiting per player
      const now = Date.now()
      const rl = rateLimit.get(playerId) || { tokens: 10, last: now }
      const elapsed = (now - rl.last) / 1000
      rl.tokens = Math.min(10, rl.tokens + elapsed * 5)
      rl.last = now
      if (rl.tokens < 1) { rateLimit.set(playerId, rl); return } // drop
      rl.tokens -= 1
      rateLimit.set(playerId, rl)
      // verify signature if present (or required)
      const idKey = playerIdentity.get(playerId)
      const requireAuth = process.env.REQUIRES_AUTH === '1'
      if (requireAuth && !msg.sig) { console.warn('missing signature from', playerId); return }
      if (msg.sig && idKey) {
        try {
          const bsv = await import('@bsv/sdk')
          const pub = bsv.PublicKey.fromString(idKey)
          const msgBytes = new TextEncoder().encode(JSON.stringify(msg.data || {}))
          const sig = bsv.Signature.fromCompact(msg.sig, 'base64')
          const valid = bsv.BSM.verify(msgBytes, sig, pub)
          if (!valid) { console.warn('invalid signature from', playerId); return }
        } catch (e) { console.warn('verify error', e.message); if (requireAuth) return }
      }
      const hostWs = room.players.get(room.hostId)
      if (hostWs && hostWs.readyState === 1) hostWs.send(JSON.stringify({ ...msg, from: playerId }))
      return
    }
    if (msg.type === 'state') {
      dlog('state from', playerId, 'room', currentRoom, 'players:', msg.snapshot?.players?.length)
      // only host can send state
      if (playerId !== room.hostId) return
      room.state = msg.snapshot
      broadcast(currentRoom, { type: 'state', room: currentRoom, hostId: playerId, snapshot: msg.snapshot }, ws)
      return
    }
    if (msg.type === 'gameOver') {
      if (playerId !== room.hostId) return
      broadcast(currentRoom, { type: 'gameOver', room: currentRoom, data: msg.data })
      return
    }
    // DiLoCo transport: checkpoint/delta relay + Hyperswarm topic
    if (typeof msg.type === 'string' && msg.type.startsWith('diloco-')) {
      // ensure swarm topic for this room (checkpoint distribution)
      if (swarm && !topics.has('diloco:' + currentRoom)) {
        try { joinRoom('diloco:' + currentRoom) } catch (e) { dlog('swarm join error', e.message) }
      }
      // relay to swarm as well
      if (swarm) {
        try {
          const buf = Buffer.from(JSON.stringify({ ...msg, room: currentRoom, from: playerId }))
          for (const conn of swarm.connections) { try { conn.write(buf) } catch (e) {} }
        } catch (e) {}
      }
      broadcast(currentRoom, { ...msg, room: currentRoom, from: playerId }, ws)
      return
    }

    // generic relay
    broadcast(currentRoom, { ...msg, room: currentRoom, from: playerId }, ws)
  })

  ws.on('close', () => {
    dlog('close', playerId, currentRoom)
    if (!currentRoom) return
    const room = rooms.get(currentRoom)
    if (!room) return
    room.players.delete(playerId)
    broadcast(currentRoom, { type: 'peer-left', room: currentRoom, playerId })
    // migrate host
    if (room.hostId === playerId && room.players.size > 0) {
      const nextHost = room.players.keys().next().value
      room.hostId = nextHost
      broadcast(currentRoom, { type: 'hostChanged', room: currentRoom, hostId: nextHost })
      const hostWs = room.players.get(nextHost)
      if (hostWs && hostWs.readyState === 1) hostWs.send(JSON.stringify({ type: 'youAreHost', room: currentRoom }))
    }
    if (room.players.size === 0) rooms.delete(currentRoom)
  })
})

server.listen(PORT, () => console.log(`Gateway listening on ${PORT}`))