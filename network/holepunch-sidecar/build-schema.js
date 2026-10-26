const Hyperschema = require('hyperschema')
const HyperDB = require('hyperdb/builder')

const SCHEMA_DIR = './spec/hyperschema'
const DB_DIR = './spec/hyperdb'

const schema = Hyperschema.from(SCHEMA_DIR)
const ns = schema.namespace('diloco')

ns.register({
  name: 'metadata',
  compact: true,
  fields: [
    { name: 'key', type: 'string', required: true },
    { name: 'value', type: 'string', required: true },
    { name: 'updated_at', type: 'uint', required: true }
  ]
})

Hyperschema.toDisk(schema)

const db = HyperDB.from(SCHEMA_DIR, DB_DIR)
const dilocoDb = db.namespace('diloco')

dilocoDb.collections.register({
  name: 'metadata',
  schema: '@diloco/metadata',
  key: ['key']
})

dilocoDb.indexes.register({
  name: 'metadata-by-key',
  collection: '@diloco/metadata',
  unique: true,
  key: {
    type: 'string',
    map: 'mapKey'
  }
})

dilocoDb.require('./helpers.js')

HyperDB.toDisk(db)
