import type { Conversation, ConversationExport, ExportedModelService } from './types'
import type { PendingRuntimeCommand, PendingRunStartCommand } from '../../runtime/client'

const STORE_NAME = 'conversations'
const PENDING_LOCAL_RUN_COMMANDS_STORE_NAME = 'pending-local-run-commands'
const PENDING_LOCAL_RUN_COMMANDS_THREAD_INDEX = 'threadId'
const DATABASE_VERSION = 3

export class LocalConversationStore {
  private dbPromise?: Promise<IDBDatabase>

  constructor(private readonly dbName = 'shejane-local') {}

  async list(): Promise<Conversation[]> {
    const store = await this.objectStore('readonly')
    const conversations = await requestToPromise<Conversation[]>(store.getAll())
    return conversations.sort(compareConversations)
  }

  async get(id: string): Promise<Conversation | undefined> {
    const store = await this.objectStore('readonly')
    return requestToPromise<Conversation | undefined>(store.get(id))
  }

  async save(conversation: Conversation): Promise<void> {
    const store = await this.objectStore('readwrite')
    await requestToPromise(store.put(conversation))
  }

  async saveWithPendingRuntimeCommand(
    conversation: Conversation,
    command: PendingRuntimeCommand,
  ): Promise<void> {
    const db = await this.open()
    const transaction = db.transaction(
      [STORE_NAME, PENDING_LOCAL_RUN_COMMANDS_STORE_NAME],
      'readwrite',
    )
    transaction.objectStore(STORE_NAME).put(conversation)
    transaction.objectStore(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME).put(command)
    await transactionToPromise(transaction)
  }

  async savePendingRuntimeCommand(command: PendingRuntimeCommand): Promise<void> {
    const store = await this.objectStoreFor(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME, 'readwrite')
    await requestToPromise(store.put(command))
  }

  async saveRuntimeProjection(conversation: Conversation): Promise<boolean> {
    const db = await this.open()
    const transaction = db.transaction(
      [STORE_NAME, PENDING_LOCAL_RUN_COMMANDS_STORE_NAME],
      'readwrite',
    )
    const conversations = transaction.objectStore(STORE_NAME)
    const commands = transaction
      .objectStore(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME)
      .index(PENDING_LOCAL_RUN_COMMANDS_THREAD_INDEX)
      .getAll(conversation.id)
    let saved = false
    commands.onsuccess = () => {
      if (!(commands.result as PendingRuntimeCommand[]).some((command) => command.canceledAt)) {
        conversations.put(conversation)
        saved = true
      }
    }
    await transactionToPromise(transaction)
    return saved
  }

  async listPendingRuntimeCommands(): Promise<PendingRuntimeCommand[]> {
    const store = await this.objectStoreFor(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME, 'readonly')
    const commands = await requestToPromise<Array<PendingRuntimeCommand | Omit<PendingRunStartCommand, 'type'>>>(store.getAll())
    return commands
      .flatMap((command) => {
        const normalized = 'type' in command ? command : { ...command, type: 'run.start' as const }
        return normalized.settledAt ? [] : [normalized]
      })
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
  }

  async getPendingRuntimeCommand(
    commandId: string,
  ): Promise<PendingRuntimeCommand | undefined> {
    const store = await this.objectStoreFor(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME, 'readonly')
    const command = await requestToPromise<
      PendingRuntimeCommand | Omit<PendingRunStartCommand, 'type'> | undefined
    >(store.get(commandId))
    if (!command) return undefined
    return 'type' in command ? command : { ...command, type: 'run.start' }
  }

  async deletePendingRuntimeCommand(commandId: string): Promise<void> {
    const store = await this.objectStoreFor(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME, 'readwrite')
    await requestToPromise(store.delete(commandId))
  }

  async settleRejectedRuntimeCommand(
    commandId: string,
    conversation?: Conversation,
  ): Promise<void> {
    const db = await this.open()
    const transaction = db.transaction(
      [STORE_NAME, PENDING_LOCAL_RUN_COMMANDS_STORE_NAME],
      'readwrite',
    )
    if (conversation) transaction.objectStore(STORE_NAME).put(conversation)
    transaction.objectStore(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME).delete(commandId)
    await transactionToPromise(transaction)
  }

  async settleCanceledLocalRunCommand(threadId: string, commandId: string): Promise<void> {
    const db = await this.open()
    const transaction = db.transaction(
      [STORE_NAME, PENDING_LOCAL_RUN_COMMANDS_STORE_NAME],
      'readwrite',
    )
    transaction.objectStore(STORE_NAME).delete(threadId)
    const commands = transaction.objectStore(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME)
    const request = commands.get(commandId)
    request.onsuccess = () => {
      if (request.result) {
        commands.put({ ...request.result, settledAt: new Date().toISOString() })
      }
    }
    await transactionToPromise(transaction)
    // ponytail: settled tombstones are tiny and thread IDs are immutable;
    // compact them only if measured storage growth justifies another lifecycle.
  }

  async delete(id: string): Promise<void> {
    const db = await this.open()
    const transaction = db.transaction(
      [STORE_NAME, PENDING_LOCAL_RUN_COMMANDS_STORE_NAME],
      'readwrite',
    )
    transaction.objectStore(STORE_NAME).delete(id)
    const pendingCommands = transaction.objectStore(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME)
    const commands = pendingCommands
      .index(PENDING_LOCAL_RUN_COMMANDS_THREAD_INDEX)
      .getAll(id)
    commands.onsuccess = () => {
      const canceledAt = new Date().toISOString()
      for (const command of commands.result as PendingRuntimeCommand[]) {
        pendingCommands.put({ ...command, canceledAt })
      }
    }
    await transactionToPromise(transaction)
  }

  async exportAll(modelServices: ExportedModelService[] = []): Promise<ConversationExport> {
    return {
      version: 1,
      exportedAt: new Date().toISOString(),
      conversations: await this.list(),
      modelServices,
    }
  }

  async importAll(payload: ConversationExport | string): Promise<ExportedModelService[]> {
    const parsed = typeof payload === 'string' ? (JSON.parse(payload) as ConversationExport) : payload
    if (parsed.version !== 1 || !Array.isArray(parsed.conversations)) {
      throw new Error('Unsupported SheJane conversation export')
    }
    const store = await this.objectStore('readwrite')
    await Promise.all(parsed.conversations.map((conversation) => requestToPromise(store.put(conversation))))
    return Array.isArray(parsed.modelServices) ? parsed.modelServices : []
  }

  private async objectStore(mode: IDBTransactionMode): Promise<IDBObjectStore> {
    return this.objectStoreFor(STORE_NAME, mode)
  }

  private async objectStoreFor(
    storeName: string,
    mode: IDBTransactionMode,
  ): Promise<IDBObjectStore> {
    const db = await this.open()
    return db.transaction(storeName, mode).objectStore(storeName)
  }

  private open(): Promise<IDBDatabase> {
    if (this.dbPromise) {
      return this.dbPromise
    }

    this.dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(this.dbName, DATABASE_VERSION)
      request.onupgradeneeded = () => {
        const db = request.result
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          const store = db.createObjectStore(STORE_NAME, { keyPath: 'id' })
          store.createIndex('updatedAt', 'updatedAt')
        }
        const pendingCommands = db.objectStoreNames.contains(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME)
          ? request.transaction!.objectStore(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME)
          : db.createObjectStore(PENDING_LOCAL_RUN_COMMANDS_STORE_NAME, {
            keyPath: 'commandId',
          })
        if (!pendingCommands.indexNames.contains(PENDING_LOCAL_RUN_COMMANDS_THREAD_INDEX)) {
          pendingCommands.createIndex(PENDING_LOCAL_RUN_COMMANDS_THREAD_INDEX, 'input.threadId')
        }
      }
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })

    return this.dbPromise
  }
}

function transactionToPromise(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error)
    transaction.onabort = () => reject(transaction.error)
  })
}

function compareConversations(a: Conversation, b: Conversation): number {
  if (Boolean(a.pinned) !== Boolean(b.pinned)) {
    return a.pinned ? -1 : 1
  }
  return b.updatedAt.localeCompare(a.updatedAt)
}

function requestToPromise<T = unknown>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

export function createLocalID(prefix: string): string {
  if (globalThis.crypto?.randomUUID) {
    return `${prefix}_${globalThis.crypto.randomUUID()}`
  }
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`
}
