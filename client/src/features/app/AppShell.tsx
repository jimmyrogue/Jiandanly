import { lazy, Suspense, useMemo } from 'react'
import { IconLayoutSidebarLeftExpand } from '@tabler/icons-react'
import { toast } from 'sonner'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ConversationSidebar } from '@/features/chat/components/ConversationSidebar'
import { PluginsHub } from '@/features/plugins/PluginsHub'
import { createLocalID } from '@/shared/local-data/localConversations'
import {
  clearLocalMemory,
  createLocalSkill,
  deleteLocalSkill,
  createMcpServer,
  deleteMcpServer,
  updateMcpServer,
  getLocalSkillFile,
  updateLocalSkill,
  advanceLocalPluginSetupCommand,
  getLocalPluginReadiness,
  getLocalFixedRuntimeAssetStatus,
  prepareLocalFixedRuntimeAsset,
  removeLocalFixedRuntimeAsset,
  getLocalRuntimeAssetStorage,
  cleanupLocalRuntimeAssetStorage,
  type FixedRuntimeAssetPluginID,
} from '@/runtime/client'
import { AppChatWorkspace } from './AppChatWorkspace'

const SkillsView = lazy(() => import('@/features/skills/SkillsView').then((module) => ({ default: module.SkillsView })))
const PluginsView = lazy(() => import('@/features/plugins/PluginsView').then((module) => ({ default: module.PluginsView })))
const MCPView = lazy(() => import('@/features/mcp/MCPView').then((module) => ({ default: module.MCPView })))
const SettingsView = lazy(() => import('@/features/settings/SettingsView').then((module) => ({ default: module.SettingsView })))

const minSidebarWidth = 190
const maxSidebarWidth = 340
const appNoticeToastID = 'app-notice-toast'

export function AppShell({ shell, chat, plugins, common }: {
  shell: Record<string, unknown>
  chat: Record<string, unknown>
  plugins: Record<string, unknown>
  common: Record<string, unknown>
}) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const {
    activeID, conversations, conversationSidebarRef,
    startNewConversation, selectConversation, renameConversation, deleteConversationData,
    togglePinConversation, exportConversationData, exportLocalData, importLocalData,
    sidebarWidth, sidebarCollapsed, sidebarMotion, isResizingSidebar,
    beginSidebarResize, handleSidebarResizeKeyDown, collapseSidebar, expandSidebar,
    shellClassName, appShellStyle,
    mainView, pluginsTab, setPluginsTab,
    keyboardHelpOpen, setKeyboardHelpOpen, shortcutRows,
  } = shell as unknown as Record<string, any>
  const {
    listInstalledSkillsForView, listMcpServersForView, listPluginsForView,
    pluginCatalogVersion, submitPluginCommand,
    agentSettings, changeAgentSettings,
    runtimeSettingsConfig, setModelCatalogVersion,
  } = plugins as unknown as Record<string, any>
  const {
    isDesktop, runtime, runtimeConnection, t, setMainView,
    modelServiceAddRequested, setModelServiceAddRequested,
  } = common as unknown as Record<string, any>

  const runtimeAssetControls = useMemo(() => runtimeConnection ? {
    getStatus: (pluginID: FixedRuntimeAssetPluginID) => (
      getLocalFixedRuntimeAssetStatus(pluginID, runtimeConnection)
    ),
    download: (pluginID: FixedRuntimeAssetPluginID) => (
      prepareLocalFixedRuntimeAsset(pluginID, runtimeConnection)
    ),
    remove: (pluginID: FixedRuntimeAssetPluginID) => (
      removeLocalFixedRuntimeAsset(pluginID, runtimeConnection)
    ),
    getStorage: () => getLocalRuntimeAssetStorage(runtimeConnection),
    cleanup: (scope: 'history' | 'all') => (
      cleanupLocalRuntimeAssetStorage(scope, runtimeConnection)
    ),
  } : null, [runtimeConnection])

  return (
    <TooltipProvider>
      <main className={shellClassName as string}>
        <div className="window-drag-layer" aria-hidden="true" />
        <div
          className="app-shell"
          style={appShellStyle as React.CSSProperties}
          data-collapsed={(sidebarCollapsed as boolean) ? 'true' : undefined}
          data-sidebar-motion={sidebarMotion !== 'idle' ? sidebarMotion as string : undefined}
        >
          <ConversationSidebar
            ref={conversationSidebarRef as any}
            conversations={conversations as any}
            activeID={activeID as string}
            onNewConversation={startNewConversation as any}
            onSelectConversation={selectConversation as any}
            onExportConversation={(id: string) => void (exportConversationData as any)(id)}
            onImportLocalData={(file: any) => void (importLocalData as any)(file)}
            onTogglePinConversation={(id: string) => void (togglePinConversation as any)(id)}
            onRenameConversation={(id: string, title: string) => void (renameConversation as any)(id, title)}
            onDeleteConversation={(id: string) => void (deleteConversationData as any)(id)}
            onCollapseSidebar={collapseSidebar as any}
            isDesktop={isDesktop as boolean}
            onOpenPlugins={() => (setMainView as any)('plugins')}
            onOpenSettings={() => (setMainView as any)('settings')}
            activeView={mainView as any}
            resizeHandle={(
              <div
                className="sidebar-resize-handle"
                role="separator"
                aria-label={(t as any)('app.resizeSidebar')}
                aria-orientation="vertical"
                aria-valuemin={minSidebarWidth}
                aria-valuemax={maxSidebarWidth}
                aria-valuenow={sidebarWidth as number}
                data-resizing={(isResizingSidebar as boolean) ? 'true' : undefined}
                tabIndex={0}
                onKeyDown={handleSidebarResizeKeyDown as any}
                onPointerDown={beginSidebarResize as any}
              />
            )}
          />

          {(sidebarCollapsed as boolean) ? (
            <div className="topbar-expand-hotspot">
              <button
                type="button"
                className="topbar-expand-button"
                title={(t as any)('app.expandSidebar')}
                aria-label={(t as any)('app.expandSidebar')}
                onClick={expandSidebar as any}
              >
                <IconLayoutSidebarLeftExpand size={16} aria-hidden="true" />
              </button>
            </div>
          ) : null}

          <div className="view-transition">
          <Suspense fallback={null}>
          {mainView === 'plugins' ? (
            <PluginsHub activeTab={pluginsTab as any} onTabChange={setPluginsTab as any}>
            <Suspense fallback={null}>
            {pluginsTab === 'skills' ? (
            <SkillsView
              embedded
              listInstalled={listInstalledSkillsForView as any}
              onCreateSkill={async (input: any) => {
                if (!runtimeConnection) return
                await createLocalSkill(input, runtimeConnection)
              }}
              onLoadSkill={(name: string) => {
                if (!runtimeConnection) return Promise.reject(new Error('Runtime unavailable'))
                return getLocalSkillFile(name, runtimeConnection)
              }}
              onUpdateSkill={async (name: string, input: any) => {
                if (!runtimeConnection) return
                await updateLocalSkill(name, input, runtimeConnection)
              }}
              onDeleteSkill={async (name: string) => {
                if (!runtimeConnection) return
                await deleteLocalSkill(name, runtimeConnection)
              }}
              onOpenFolder={(path: string) => {
                const bridge = window.shejaneClient
                if (bridge?.openFileWithDefaultApp) {
                  void bridge.openFileWithDefaultApp(path)
                }
              }}
            />
          ) : pluginsTab === 'plugins' ? (
            <PluginsView
              embedded
              refreshVersion={pluginCatalogVersion as number}
              listPlugins={listPluginsForView as any}
              selectPackage={() => window.shejaneClient?.selectPluginPackage?.() ?? Promise.resolve(undefined)}
              installPlugin={(sourcePath: string, allowUnsigned: boolean) => {
                const commandId = createLocalID('cmd')
                return (submitPluginCommand as any)({
                  type: 'plugin.install',
                  commandId,
                  createdAt: new Date().toISOString(),
                  input: { sourcePath, allowUnsigned },
                })
              }}
              setEnabled={(plugin: { id: string, digest: string }, enabled: boolean) => {
                const commandId = createLocalID('cmd')
                return (submitPluginCommand as any)({
                  type: enabled ? 'plugin.enable' : 'plugin.disable',
                  commandId,
                  createdAt: new Date().toISOString(),
                  input: { pluginId: plugin.id, expectedDigest: plugin.digest },
                })
              }}
              getReadiness={(plugin: { id: string }) => {
                if (!runtimeConnection) return Promise.reject(new Error('Runtime unavailable'))
                return getLocalPluginReadiness(plugin.id, runtimeConnection)
              }}
              advanceSetup={(plugin: { id: string }, readiness: { revision: number }, actionId: any) => {
                if (!runtimeConnection) return Promise.reject(new Error('Runtime unavailable'))
                const commandId = createLocalID('cmd')
                return advanceLocalPluginSetupCommand(
                  commandId,
                  plugin.id as 'org.shejane.computer-use',
                  readiness.revision,
                  actionId,
                  runtimeConnection,
                )
              }}
              removePlugin={(plugin: { id: string, digest: string }) => {
                const commandId = createLocalID('cmd')
                return (submitPluginCommand as any)({
                  type: 'plugin.remove',
                  commandId,
                  createdAt: new Date().toISOString(),
                  input: { pluginId: plugin.id, expectedDigest: plugin.digest },
                })
              }}
            />
          ) : (
            <MCPView
              embedded
              listCatalog={listMcpServersForView as any}
              disabledServers={(agentSettings as any)?.mcpDisabled ?? []}
              onDisabledChange={(next: string[]) => {
                ;(changeAgentSettings as any)({ ...agentSettings as any, mcpDisabled: next })
              }}
              onCreateServer={async (input: any) => {
                if (!runtimeConnection) return
                await createMcpServer(input, runtimeConnection)
              }}
              onUpdateServer={async (name: string, input: any) => {
                if (!runtimeConnection) return
                await updateMcpServer(name, input, runtimeConnection)
              }}
              onDeleteServer={async (name: string) => {
                if (!runtimeConnection) return
                await deleteMcpServer(name, runtimeConnection)
              }}
              onOpenFolder={(path: string) => {
                const bridge = window.shejaneClient
                if (bridge?.openFileWithDefaultApp) {
                  void bridge.openFileWithDefaultApp(path)
                }
              }}
            />
          )}
            </Suspense>
            </PluginsHub>
          ) : mainView === 'settings' ? (
            <SettingsView
              isDesktop={isDesktop as boolean}
              agentSettings={agentSettings as any}
              advancedSettingsReady={runtimeSettingsConfig === runtimeConnection && Boolean((runtime as any)?.online)}
              runtimeConnection={runtimeConnection as any}
              getRuntimeAssetStatus={runtimeAssetControls?.getStatus}
              onDownloadRuntimeAsset={runtimeAssetControls?.download}
              onRemoveRuntimeAsset={runtimeAssetControls?.remove}
              getRuntimeAssetStorage={runtimeAssetControls?.getStorage}
              onCleanupRuntimeAssets={runtimeAssetControls?.cleanup}
              openModelServiceAdd={modelServiceAddRequested as boolean}
              onModelServiceAddOpened={() => (setModelServiceAddRequested as any)(false)}
              onModelServicesChange={setModelCatalogVersion as any}
              onAgentSettingsChange={(next: any) => {
                (changeAgentSettings as any)(next)
              }}
              onImportLocalData={(file: any) => void (importLocalData as any)(file)}
              onExportLocalData={() => void (exportLocalData as any)()}
              onClearMemory={
                runtimeConnection
                  ? async () => {
                      try {
                        const result = await clearLocalMemory(runtimeConnection)
                        toast.success((t as any)('app.notice.memoryCleared', { count: result.deleted_count }), { id: appNoticeToastID })
                        return result.deleted_count
                      } catch (error) {
                        const message = error instanceof Error ? error.message : String(error)
                        toast.error((t as any)('app.notice.memoryClearFailed', { message }), { id: appNoticeToastID })
                        throw error
                      }
                    }
                  : undefined
              }
            />
          ) : (
            <AppChatWorkspace chat={chat} common={common} />
          )}
          </Suspense>
          </div>
          <Dialog open={keyboardHelpOpen as boolean} onOpenChange={setKeyboardHelpOpen as any}>
            <DialogContent className="keyboard-shortcuts-dialog sm:max-w-[420px]">
              <DialogHeader>
                <DialogTitle>{(t as any)('shortcuts.title')}</DialogTitle>
                <DialogDescription>{(t as any)('shortcuts.description')}</DialogDescription>
              </DialogHeader>
              <div className="keyboard-shortcuts-list">
                {(shortcutRows as any[]).map((row: any) => (
                  <div className="keyboard-shortcut-row" key={row.label}>
                    <span>{row.label}</span>
                    <span className="keyboard-shortcut-keys">
                      {row.keys.map((key: string) => (
                        <kbd key={key}>{key}</kbd>
                      ))}
                    </span>
                  </div>
                ))}
              </div>
            </DialogContent>
          </Dialog>
        </div>
      </main>
    </TooltipProvider>
  )
}
