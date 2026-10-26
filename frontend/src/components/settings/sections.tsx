import { useState, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Check,
  Copy,
  Download,
  Eye,
  EyeOff,
  LogOut,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react'
import { apiClient, unwrap } from '@/lib/api-client'
import {
  useActiveBulkJob,
  useBulkLocalize,
  useCancelMediaJob,
  useMediaJob,
} from '@/hooks/use-bulk-media'
import {
  useApiToken,
  useLogout,
  useSecurity,
  useUpdateSecurity,
} from '@/hooks/use-auth'
import { useArchiveStats } from '@/hooks/use-characters'
import { useRescan } from '@/hooks/use-rescan'
import { useDatacatFollows } from '@/hooks/use-discover'
import {
  useProviderSettings,
  useSettings,
  useUpdateProxyUrl,
  useUpdateRoot,
  useUpdateUi2,
} from '@/hooks/use-settings'
import {
  datacatClearToken,
  datacatRefreshToken,
  datacatValidate,
  type DatacatSessionState,
} from '@/lib/providers/datacat'
import { formatBytes } from '@/lib/card'
import { copyText } from '@/lib/clipboard'
import { SORTS } from '@/lib/browse'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'
import {
  OptionRow,
  SelectField,
  SettingsSection,
  Stat,
  Toggle,
} from './controls'

// ---- Security ---------------------------------------------------------------

/**
 * The login gate (`proxy/api/gate.py`): one username and password in front of
 * the whole server. Not accounts — there is one set of credentials and no
 * notion of who is logged in beyond "someone who knew them".
 *
 * Unlike every other section this one has an explicit Save and does not write
 * as you type: a half-typed password must never become the password, and
 * switching the gate on is only valid together with the credentials that open
 * it. The password field is write-only — the server keeps a hash and has
 * nothing to show — so blank means "leave it as it is".
 */
export function SecuritySection() {
  const security = useSecurity()
  const save = useUpdateSecurity()
  const logout = useLogout()
  const token = useApiToken()
  const stored = security.data

  const [enabledDraft, setEnabledDraft] = useState<boolean | null>(null)
  const [usernameDraft, setUsernameDraft] = useState<string | null>(null)
  const [password, setPassword] = useState('')

  const enabled = enabledDraft ?? stored?.enabled ?? false
  const username = usernameDraft ?? stored?.username ?? ''
  const hasPassword = Boolean(password) || Boolean(stored?.has_password)
  const dirty =
    enabled !== (stored?.enabled ?? false) ||
    username.trim() !== (stored?.username ?? '') ||
    Boolean(password)
  const incomplete = enabled && !(username.trim() && hasPassword)

  const field =
    'h-[31px] w-[220px] rounded-lg border border-line bg-raised px-2.5 text-[12.5px] text-text outline-none focus:border-sage'

  return (
    <SettingsSection
      title="Security"
      lede="A login in front of the archive. Off by default."
    >
      <OptionRow
        label="Require login"
        hint="Covers the whole server — this app, the API and the userscript endpoints."
      >
        <Toggle
          on={enabled}
          onChange={setEnabledDraft}
          disabled={!stored || save.isPending}
        />
      </OptionRow>
      <OptionRow label="Username">
        <input
          value={username}
          onChange={(e) => setUsernameDraft(e.target.value)}
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          className={field}
        />
      </OptionRow>
      <OptionRow
        label="Password"
        hint={
          stored?.has_password
            ? 'Leave blank to keep the current password.'
            : 'Not set.'
        }
      >
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={stored?.has_password ? 'unchanged' : 'not set'}
          autoComplete="new-password"
          className={field}
        />
      </OptionRow>
      <OptionRow
        label="API token"
        hint={
          stored?.api_token
            ? 'Built into the bridges Settings → Userscripts generates.'
            : 'Lets the userscripts through without a login. None yet.'
        }
      >
        {stored?.api_token && (
          <>
            <input
              readOnly
              value={stored.api_token}
              onFocus={(e) => e.target.select()}
              className={cn(field, 'font-mono text-[11.5px]')}
            />
            <button
              type="button"
              title="Copy"
              onClick={() =>
                void copyText(stored.api_token!).then((ok) =>
                  ok
                    ? toast('Token copied.')
                    : toast('Could not copy the token.', 'bad'),
                )
              }
              className="flex size-[31px] items-center justify-center rounded-lg border border-line text-muted hover:border-white/20 hover:text-text"
            >
              <Copy className="size-3.5" />
            </button>
          </>
        )}
        <button
          type="button"
          disabled={!stored || token.isPending}
          onClick={() =>
            token.mutate(
              {},
              {
                onSuccess: () =>
                  toast(
                    stored?.api_token
                      ? 'New token — regenerate and reinstall the userscripts.'
                      : 'Token generated — generate the userscripts to pick it up.',
                  ),
                onError: (err) => toast(err.message, 'bad'),
              },
            )
          }
          className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text disabled:opacity-60"
        >
          <RefreshCw className="size-3.5" />
          {stored?.api_token ? 'Regenerate' : 'Generate'}
        </button>
        {stored?.api_token && (
          <button
            type="button"
            disabled={token.isPending}
            onClick={() =>
              token.mutate(
                { revoke: true },
                {
                  onSuccess: () => toast('Token revoked.'),
                  onError: (err) => toast(err.message, 'bad'),
                },
              )
            }
            className="h-[31px] rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-bad/40 hover:text-bad disabled:opacity-60"
          >
            Revoke
          </button>
        )}
      </OptionRow>
      <div className="py-[13px]">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={!stored || !dirty || incomplete || save.isPending}
            onClick={() =>
              save.mutate(
                { enabled, username, password: password || null },
                {
                  onSuccess: (saved) => {
                    toast(
                      saved.enabled ? 'Login required.' : 'Login not required.',
                    )
                    setEnabledDraft(null)
                    setUsernameDraft(null)
                    setPassword('')
                  },
                  onError: (err) => toast(err.message, 'bad'),
                },
              )
            }
            className="flex h-[31px] items-center gap-1.5 rounded-lg border border-sage-line bg-sage-dim px-3 text-[12.5px] text-sage hover:bg-sage-dim/70 disabled:opacity-50"
          >
            <Check className="size-3.5" />
            Save
          </button>
          {stored?.enabled && (
            <button
              type="button"
              disabled={logout.isPending}
              onClick={() =>
                logout.mutate(undefined, {
                  // A reload rather than a state flip: it drops everything the
                  // session had on screen and lands on the login screen.
                  onSuccess: () => window.location.reload(),
                  onError: (err) => toast(err.message, 'bad'),
                })
              }
              className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text"
            >
              <LogOut className="size-3.5" />
              Log out
            </button>
          )}
          {incomplete && (
            <span className="text-[11.5px] text-faint">
              Set a username and a password to enable login.
            </span>
          )}
        </div>
        <p className="mt-2.5 text-[11.5px] text-faint">
          Saving new credentials logs out every other browser; the API token
          keeps working until it is regenerated or revoked. Locked out? Delete{' '}
          <span className="font-mono">data/security.json</span> on the server.
        </p>
      </div>
    </SettingsSection>
  )
}

// ---- Library ----------------------------------------------------------------

export function LibrarySection() {
  const settings = useSettings()
  const update = useUpdateUi2()
  const ui2 = settings.data?.ui2

  return (
    <SettingsSection
      title="Library"
      lede="How the grid looks and what the Characters tab shows by default."
    >
      <OptionRow
        label="Default sort"
        hint="Applied when the Characters tab opens with no sort of its own."
      >
        <SelectField
          value={ui2?.defaultSort ?? 'name'}
          onChange={(value) => update.mutate({ key: 'defaultSort', value })}
          options={SORTS.map((s) => ({ value: s.value, label: s.label }))}
        />
      </OptionRow>
      <OptionRow
        label="Title tiles with listing names"
        hint="The source page's tagline instead of the character's name. Also toggled by clicking the Characters heading."
      >
        <Toggle
          on={ui2?.showListingNames === true}
          onChange={(value) =>
            update.mutate({ key: 'showListingNames', value })
          }
        />
      </OptionRow>
      <OptionRow
        label='Show "Recently added" shelf'
        hint="A single fixed row above the grid. Also toggled by its own Hide button."
      >
        <Toggle
          on={ui2?.showRecentShelf !== false}
          onChange={(value) => update.mutate({ key: 'showRecentShelf', value })}
        />
      </OptionRow>
    </SettingsSection>
  )
}

// ---- Archive & storage --------------------------------------------------------

export function ArchiveSection() {
  const stats = useArchiveStats()
  const data = stats.data
  const totalBytes = data ? data.bytes + data.media.bytes : undefined

  return (
    <SettingsSection
      title="Archive & storage"
      lede="Where cards live on disk. One mount, one path."
    >
      <div className="flex flex-wrap gap-x-8 gap-y-3 py-[13px]">
        <Stat value={data ? data.cards.toLocaleString() : '—'} label="cards" />
        <Stat
          value={totalBytes !== undefined ? formatBytes(totalBytes) : '—'}
          label="on disk"
        />
        <Stat
          value={data ? data.galleries.toLocaleString() : '—'}
          label="galleries"
        />
        <Stat
          value={data ? data.media.images.toLocaleString() : '—'}
          label="gallery images"
        />
        <Stat
          value={data ? data.unreadable.toLocaleString() : '—'}
          label="broken cards"
        />
      </div>
      <OptionRow label="Data directory" hint={data?.archive_dir ?? '—'} />
      <OptionRow
        label="Characters"
        hint={
          data
            ? `${formatBytes(data.bytes)} across ${data.cards.toLocaleString()} cards.`
            : '—'
        }
      />
      <OptionRow
        label="Galleries"
        hint={
          data
            ? `${formatBytes(data.media.bytes)} across ${data.media.images.toLocaleString()} images in ${data.galleries.toLocaleString()} folders${
                data.media.by_ext.length
                  ? ` — ${data.media.by_ext
                      .map((e) => `${e.count.toLocaleString()} ${e.ext}`)
                      .join(', ')}`
                  : ''
              }.`
            : '—'
        }
      />
      <OptionRow
        label="Avatar compression"
        hint="pngquant, ~56% smaller and visually lossless — always on at import, not a per-card choice."
      />
      <OptionRow
        label="Duplicate handling"
        hint="Cards are keyed by their id fragment (never by name) and an import that matches one already on disk is skipped."
      />
    </SettingsSection>
  )
}

// ---- Providers ----------------------------------------------------------------

type ProxyStatus = {
  configured: boolean
  url?: string | null
  state: 'unset' | 'ok' | 'bypassed' | 'error'
  proxy_ip?: string | null
  direct_ip?: string | null
  latency_ms?: number | null
  error?: string | null
}

const STATE_COLOR: Record<ProxyStatus['state'], string> = {
  ok: 'bg-sage',
  bypassed: 'bg-warn',
  error: 'bg-bad',
  unset: 'bg-faint',
}

const STATE_LABEL: Record<ProxyStatus['state'], string> = {
  ok: 'active, IP differs',
  bypassed: 'configured, but the IP did not change',
  error: 'failed',
  unset: 'not configured',
}

export function ProvidersSection() {
  const settings = useSettings()
  const update = useUpdateUi2()
  const updateProxy = useUpdateProxyUrl()
  const updateRoot = useUpdateRoot()
  const providers = useProviderSettings()
  const ui2 = settings.data?.ui2

  const [draft, setDraft] = useState<string | null>(null)
  const storedUrl = (settings.data?.httpProxyUrl as string | undefined) ?? ''
  const url = draft ?? storedUrl

  const test = useMutation({
    mutationFn: (candidate: string) =>
      unwrap(
        apiClient.GET('/api/v1/proxy/status', {
          params: { query: candidate ? { url: candidate } : {} },
        }),
        'could not test the proxy',
      ) as Promise<ProxyStatus>,
  })

  return (
    <SettingsSection
      title="Providers"
      lede="Sources the Discover tab searches, and the outbound proxy they go through."
    >
      <OptionRow
        label="Chub"
        hint="Browsing is anonymous; a token unlocks the Following feed."
      >
        <Toggle
          on={ui2?.providers?.chub !== false}
          onChange={(value) =>
            update.mutate({
              key: 'providers',
              value: { ...ui2?.providers, chub: value },
            })
          }
        />
      </OptionRow>
      <ChubTokenRow />
      <OptionRow
        label="Show NSFW from Chub"
        hint="Sends Chub’s nsfw and nsfl flags together."
      >
        <Toggle
          on={providers.chubNsfw}
          onChange={(value) => updateRoot.mutate({ chubNsfw: value })}
        />
      </OptionRow>

      <OptionRow label="DataCat" hint="Anonymous session token">
        <Toggle
          on={ui2?.providers?.datacat !== false}
          onChange={(value) =>
            update.mutate({
              key: 'providers',
              value: { ...ui2?.providers, datacat: value },
            })
          }
        />
      </OptionRow>
      <DatacatSessionRow />
      <OptionRow
        label="Show NSFW from DataCat"
        hint="Applies to Discover results from DataCat."
      >
        <Toggle
          on={providers.datacatNsfw}
          onChange={(value) => updateRoot.mutate({ datacatNsfw: value })}
        />
      </OptionRow>
      <DatacatFollowingRow />
      <div className="py-[13px]">
        <div className="text-[13.5px]">Outbound proxy</div>
        <p className="mt-0.5 text-[11.5px] text-faint">
          Every server-initiated fetch (media downloads, provider lookups)
          routes through this. Empty means direct.
        </p>
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <input
            value={url}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="http://host:port or socks5://host:port"
            className="h-[31px] min-w-[260px] flex-1 rounded-lg border border-line bg-raised px-2.5 font-mono text-[12px] text-text outline-none focus:border-sage"
          />
          <button
            type="button"
            onClick={() => test.mutate(url)}
            disabled={test.isPending}
            className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text disabled:opacity-60"
          >
            <RefreshCw
              className={cn('size-3.5', test.isPending && 'animate-spin')}
            />
            Test
          </button>
          <button
            type="button"
            onClick={() =>
              updateProxy.mutate(url, {
                onSuccess: () => {
                  toast('Proxy URL saved.')
                  setDraft(null)
                },
                onError: (err) => toast(err.message, 'bad'),
              })
            }
            disabled={updateProxy.isPending || url === storedUrl}
            className="flex h-[31px] items-center gap-1.5 rounded-lg border border-sage-line bg-sage-dim px-3 text-[12.5px] text-sage hover:bg-sage-dim/70 disabled:opacity-50"
          >
            <Check className="size-3.5" />
            Save
          </button>
        </div>
        {test.data && (
          <div className="mt-2.5 flex items-center gap-2 font-mono text-[11.5px] text-faint">
            <span
              className={cn(
                'size-2 flex-none rounded-full',
                STATE_COLOR[test.data.state],
              )}
            />
            {STATE_LABEL[test.data.state]}
            {test.data.proxy_ip && ` · proxy ${test.data.proxy_ip}`}
            {test.data.direct_ip && ` · direct ${test.data.direct_ip}`}
            {test.data.latency_ms !== undefined &&
              test.data.latency_ms !== null &&
              ` · ${test.data.latency_ms}ms`}
            {test.data.error && ` · ${test.data.error}`}
          </div>
        )}
        {test.error && (
          <p className="mt-2 text-[11.5px] text-bad">
            {(test.error as Error).message}
          </p>
        )}
      </div>
    </SettingsSection>
  )
}

/**
 * A root-level credential in `data/settings.json`: masked input, show/hide,
 * and a Save that writes `null` when cleared rather than an empty string.
 *
 * Extracted when Civitai needed a second one of these. `children` is for the
 * extra control a particular credential wants under the field — Chub's
 * "keep on reset" checkbox — so the shared shape stays shared without the
 * component learning about any specific provider.
 */
function SecretRow({
  label,
  stored,
  settingsKey,
  savedMessage,
  clearedMessage,
  hint,
  children,
}: {
  label: string
  stored: string | null
  settingsKey: string
  savedMessage: string
  clearedMessage: string
  hint: ReactNode
  children?: ReactNode
}) {
  const updateRoot = useUpdateRoot()
  const [draft, setDraft] = useState<string | null>(null)
  const [shown, setShown] = useState(false)
  const value = draft ?? stored ?? ''

  return (
    <div className="py-[13px]">
      <div className="text-[13.5px]">{label}</div>
      <p className="mt-0.5 text-[11.5px] text-faint">{hint}</p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <input
          type={shown ? 'text' : 'password'}
          value={value}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="not set"
          autoComplete="off"
          spellCheck={false}
          className="h-[31px] min-w-[260px] flex-1 rounded-lg border border-line bg-raised px-2.5 font-mono text-[12px] text-text outline-none focus:border-sage"
        />
        <button
          type="button"
          onClick={() => setShown((v) => !v)}
          className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text"
        >
          {shown ? (
            <EyeOff className="size-3.5" />
          ) : (
            <Eye className="size-3.5" />
          )}
          {shown ? 'Hide' : 'Show'}
        </button>
        <button
          type="button"
          disabled={updateRoot.isPending || value === (stored ?? '')}
          onClick={() =>
            updateRoot.mutate(
              { [settingsKey]: value.trim() || null },
              {
                onSuccess: () => {
                  toast(value.trim() ? savedMessage : clearedMessage)
                  setDraft(null)
                },
                onError: (err) => toast(err.message, 'bad'),
              },
            )
          }
          className="flex h-[31px] items-center gap-1.5 rounded-lg border border-sage-line bg-sage-dim px-3 text-[12.5px] text-sage hover:bg-sage-dim/70 disabled:opacity-50"
        >
          <Check className="size-3.5" />
          Save
        </button>
      </div>
      {children}
    </div>
  )
}

/**
 * Chub's URQL token.
 *
 * Restored at Stage 6B — it vanished in the rewrite, taking the Following feed
 * with it, and this section spent a stage claiming Chub needed "No API key".
 * The name is chub.ai's, not ours: their site stores the session token in
 * localStorage under `URQL_TOKEN` because it uses urql as its GraphQL client.
 * Nothing here speaks GraphQL — the value is a plain bearer token.
 */
function ChubTokenRow() {
  const { chubToken, chubRememberToken } = useProviderSettings()
  const updateRoot = useUpdateRoot()

  return (
    <SecretRow
      label="Chub URQL token"
      stored={chubToken}
      settingsKey="chubToken"
      savedMessage="Chub token saved."
      clearedMessage="Chub token cleared."
      hint={
        <>
          Sign in at chub.ai, then copy the{' '}
          <span className="font-mono">URQL_TOKEN</span> value from the site’s
          local storage. Needed only for the Following feed; browsing and adding
          cards work without it.
        </>
      }
    >
      <label className="mt-2.5 flex items-center gap-2 text-[11.5px] text-faint">
        <input
          type="checkbox"
          checked={chubRememberToken}
          onChange={(e) =>
            updateRoot.mutate({ chubRememberToken: e.target.checked })
          }
          className="size-3.5 accent-[var(--sage)]"
        />
        Keep this token if settings are reset
      </label>
    </SecretRow>
  )
}

/**
 * Civitai's API key.
 *
 * Lives under Media rather than Providers because Civitai is not a card
 * source — it is one of the gallery extractors
 * (`proxy/media/civitai.py`), reached only when a card links a post.
 * Public posts need no key at all; a key is what reaches content Civitai
 * hides behind a login.
 */
function CivitaiKeyRow() {
  const { civitaiApiKey } = useProviderSettings()

  return (
    <SecretRow
      label="Civitai API key"
      stored={civitaiApiKey}
      settingsKey="civitaiApiKey"
      savedMessage="Civitai API key saved."
      clearedMessage="Civitai API key cleared."
      hint={
        <>
          Optional. Cards linking a{' '}
          <span className="font-mono">civitai.com/posts/…</span> gallery
          download fine without one; a key is only needed for images Civitai
          hides from logged-out visitors. Create one under Account → API Keys.
        </>
      }
    />
  )
}

/**
 * DataCat's anonymous session.
 *
 * The server holds the token in process memory only
 * (`proxy/sources/datacat_client.py:239-244`), so before Stage 6B a restart
 * silently minted a new anonymous identity with no way to see or steer it.
 * Saving persists it into the settings blob and pushes it back into the
 * server's session on load.
 */
function DatacatSessionRow() {
  const { datacatToken } = useProviderSettings()
  const updateRoot = useUpdateRoot()
  const [state, setState] = useState<DatacatSessionState | null>(null)
  const [busy, setBusy] = useState<'check' | 'new' | 'clear' | null>(null)

  const check = async () => {
    setBusy('check')
    try {
      setState(await datacatValidate())
    } finally {
      setBusy(null)
    }
  }

  const mint = async () => {
    setBusy('new')
    try {
      const token = await datacatRefreshToken()
      if (!token) {
        toast('DataCat did not return a token.', 'bad')
        return
      }
      await updateRoot.mutateAsync({ datacatToken: token })
      setState(await datacatValidate())
      toast('New DataCat session token.')
    } catch (err) {
      toast((err as Error).message, 'bad')
    } finally {
      setBusy(null)
    }
  }

  const clear = async () => {
    setBusy('clear')
    try {
      await datacatClearToken()
      await updateRoot.mutateAsync({ datacatToken: null })
      setState({ valid: false, reason: 'cleared' })
      toast('DataCat session cleared.')
    } catch (err) {
      toast((err as Error).message, 'bad')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="py-[13px]">
      <div className="text-[13.5px]">DataCat session</div>
      <p className="mt-0.5 text-[11.5px] text-faint">
        Anonymous — no account, no credentials. Saved here so it survives a
        server restart instead of being reissued silently.
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <span className="min-w-[260px] flex-1 truncate rounded-lg border border-line bg-raised px-2.5 py-[7px] font-mono text-[12px] text-faint">
          {datacatToken
            ? `${datacatToken.slice(0, 12)}…${datacatToken.slice(-6)}`
            : 'no token saved'}
        </span>
        <button
          type="button"
          onClick={() => void check()}
          disabled={busy !== null}
          className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text disabled:opacity-60"
        >
          <RefreshCw
            className={cn('size-3.5', busy === 'check' && 'animate-spin')}
          />
          Test
        </button>
        <button
          type="button"
          onClick={() => void mint()}
          disabled={busy !== null}
          className="h-[31px] rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text disabled:opacity-60"
        >
          {busy === 'new' ? 'Requesting…' : 'New token'}
        </button>
        <button
          type="button"
          onClick={() => void clear()}
          disabled={busy !== null || !datacatToken}
          className="h-[31px] rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-bad/40 hover:text-bad disabled:opacity-40"
        >
          Clear
        </button>
      </div>
      {state && (
        <div className="mt-2.5 flex items-center gap-2 font-mono text-[11.5px] text-faint">
          <span
            className={cn(
              'size-2 flex-none rounded-full',
              state.valid ? 'bg-sage' : 'bg-bad',
            )}
          />
          {state.valid
            ? `valid${state.totalCount ? ` · ${state.totalCount.toLocaleString()} cards reachable` : ''}`
            : `invalid · ${state.reason ?? 'unknown'}`}
        </div>
      )}
    </div>
  )
}

/**
 * DataCat's followed creators.
 *
 * DataCat has no accounts, so unlike Chub there is no remote list to mirror —
 * this *is* the list. That asymmetry is why follow/unfollow is built here in
 * full while Chub's Following stays read-only.
 */
function DatacatFollowingRow() {
  const { creators, follow, unfollow } = useDatacatFollows()
  const [input, setInput] = useState('')

  const add = () =>
    follow.mutate(input, {
      onSuccess: (row) => {
        toast(`Following ${row.name}.`)
        setInput('')
      },
      onError: (err) => toast(err.message, 'bad'),
    })

  return (
    <div className="py-[13px]">
      <div className="text-[13.5px]">DataCat following</div>
      <p className="mt-0.5 text-[11.5px] text-faint">
        Creators whose cards show up under Discover → Following. Paste a creator
        profile URL or id.
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && input.trim()) add()
          }}
          placeholder="https://datacat.run/creators/… or a uuid"
          className="h-[31px] min-w-[260px] flex-1 rounded-lg border border-line bg-raised px-2.5 font-mono text-[12px] text-text outline-none focus:border-sage"
        />
        <button
          type="button"
          onClick={add}
          disabled={!input.trim() || follow.isPending}
          className="flex h-[31px] items-center gap-1.5 rounded-lg border border-sage-line bg-sage-dim px-3 text-[12.5px] text-sage hover:bg-sage-dim/70 disabled:opacity-50"
        >
          <Plus className="size-3.5" />
          {follow.isPending ? 'Adding…' : 'Follow'}
        </button>
      </div>
      {creators.length === 0 ? (
        <p className="mt-2.5 text-[11.5px] text-faint">
          Not following anyone yet.
        </p>
      ) : (
        <ul className="mt-2.5 flex flex-wrap gap-1.5">
          {creators.map((creator) => (
            <li
              key={creator.id}
              className="flex items-center gap-1.5 rounded-full border border-line bg-raised py-1 pr-1.5 pl-3 text-[12.5px]"
            >
              {creator.name}
              <button
                type="button"
                onClick={() =>
                  unfollow.mutate(creator.id, {
                    onSuccess: () => toast(`Unfollowed ${creator.name}.`),
                    onError: (err) => toast(err.message, 'bad'),
                  })
                }
                aria-label={`Unfollow ${creator.name}`}
                className="grid size-5 place-items-center rounded-full text-faint hover:bg-white/10 hover:text-bad"
              >
                <X className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---- Userscripts ----------------------------------------------------------------

type UserscriptSpec = {
  key: string
  label: string
  site: string
  filename: string
  description: string
  supports_tag_filter: boolean
}

export function UserscriptsSection() {
  const specs = useQuery({
    queryKey: ['userscripts'],
    staleTime: 5 * 60_000,
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/userscripts'),
        'could not list userscripts',
      ) as Promise<UserscriptSpec[]>,
  })

  const [key, setKey] = useState<string | null>(null)
  const [serverUrl, setServerUrl] = useState(window.location.origin)
  const [includeTags, setIncludeTags] = useState('')
  const [excludeTags, setExcludeTags] = useState('')
  const [copied, setCopied] = useState(false)

  const active = specs.data?.find((s) => s.key === key) ?? specs.data?.[0]
  const security = useSecurity()
  const needsToken = security.data?.enabled && !security.data.api_token

  const generate = useMutation({
    mutationFn: () => {
      if (!active) throw new Error('no bridge selected')
      const parse = (raw: string) =>
        raw.trim()
          ? raw
              .split(',')
              .map((t) => t.trim())
              .filter(Boolean)
          : null
      return unwrap(
        apiClient.POST('/api/v1/userscripts/{key}', {
          params: { path: { key: active.key } },
          body: {
            server_url: serverUrl.trim() || null,
            include_tags: active.supports_tag_filter
              ? parse(includeTags)
              : null,
            exclude_tags: active.supports_tag_filter
              ? parse(excludeTags)
              : null,
          },
        }),
        'could not generate the bridge',
      )
    },
    onError: (err) => toast(err.message, 'bad'),
  })

  const download = () => {
    if (!generate.data) return
    const blob = new Blob([generate.data.source], { type: 'text/javascript' })
    const href = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = href
    a.download = generate.data.filename
    a.click()
    URL.revokeObjectURL(href)
  }

  return (
    <SettingsSection
      title="Userscripts"
      lede="A ready-to-paste Tampermonkey bridge for each site this archive imports from."
    >
      <div className="flex flex-col gap-4 py-[13px]">
        {specs.isPending && <p className="text-[13px] text-faint">reading…</p>}
        {specs.data && (
          <div className="flex flex-wrap gap-2">
            {specs.data.map((s) => (
              <button
                key={s.key}
                type="button"
                onClick={() => setKey(s.key)}
                className={cn(
                  'rounded-full border px-3.5 py-1.5 text-[12.5px]',
                  s.key === active?.key
                    ? 'border-sage-line bg-sage-dim text-sage'
                    : 'border-line text-muted hover:border-white/20 hover:text-text',
                )}
              >
                {s.label}
              </button>
            ))}
          </div>
        )}
        {active && (
          <>
            <p className="text-[12.5px] text-faint">
              {active.description} Runs on{' '}
              <span className="font-mono">{active.site}</span>, saved as{' '}
              <span className="font-mono">{active.filename}</span>.
            </p>
            <label className="flex flex-col gap-1.5 text-[12.5px] text-muted">
              Server URL
              <input
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                className="h-[33px] rounded-lg border border-line bg-raised px-2.5 font-mono text-[12px] text-text outline-none focus:border-sage"
              />
            </label>
            {active.supports_tag_filter && (
              <div className="flex flex-wrap gap-3">
                <label className="flex flex-1 flex-col gap-1.5 text-[12.5px] text-muted">
                  Include tags (comma-separated)
                  <input
                    value={includeTags}
                    onChange={(e) => setIncludeTags(e.target.value)}
                    className="h-[33px] rounded-lg border border-line bg-raised px-2.5 text-[12.5px] text-text outline-none focus:border-sage"
                  />
                </label>
                <label className="flex flex-1 flex-col gap-1.5 text-[12.5px] text-muted">
                  Exclude tags (comma-separated)
                  <input
                    value={excludeTags}
                    onChange={(e) => setExcludeTags(e.target.value)}
                    className="h-[33px] rounded-lg border border-line bg-raised px-2.5 text-[12.5px] text-text outline-none focus:border-sage"
                  />
                </label>
              </div>
            )}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => generate.mutate()}
                disabled={generate.isPending}
                className="flex h-[33px] items-center gap-1.5 rounded-lg border border-sage-line bg-sage-dim px-3.5 text-[12.5px] font-medium text-sage hover:bg-sage-dim/70 disabled:opacity-60"
              >
                Generate
              </button>
            </div>
            {needsToken && (
              <p className="text-[12px] text-bad">
                Login is on and there is no API token, so this bridge would be
                refused. Generate one under Security first.
              </p>
            )}
            {generate.data?.includes_token && (
              <p className="text-[12px] text-faint">
                The API token is built in — nothing to set in Tampermonkey.
                Treat the script like a password.
              </p>
            )}
            {generate.data && (
              <div className="rounded-xl border border-line-soft bg-raised">
                <div className="flex items-center justify-between border-b border-line-soft px-3.5 py-2">
                  <span className="font-mono text-[11.5px] text-faint">
                    {generate.data.filename} ·{' '}
                    {generate.data.bytes.toLocaleString()} bytes
                  </span>
                  <div className="flex gap-1.5">
                    <button
                      type="button"
                      onClick={() => {
                        void copyText(generate.data!.source).then((ok) => {
                          if (!ok)
                            return toast(
                              'Could not copy — use Download instead.',
                              'bad',
                            )
                          setCopied(true)
                          setTimeout(() => setCopied(false), 1500)
                        })
                      }}
                      className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11.5px] text-muted hover:text-text"
                    >
                      {copied ? (
                        <Check className="size-3" />
                      ) : (
                        <Copy className="size-3" />
                      )}
                      {copied ? 'Copied' : 'Copy'}
                    </button>
                    <button
                      type="button"
                      onClick={download}
                      className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11.5px] text-muted hover:text-text"
                    >
                      <Download className="size-3" />
                      Download
                    </button>
                  </div>
                </div>
                <pre className="max-h-[280px] overflow-auto px-3.5 py-3 font-mono text-[11px] leading-[1.6] text-[#c3cacd]">
                  {generate.data.source}
                </pre>
              </div>
            )}
          </>
        )}
      </div>
    </SettingsSection>
  )
}

// ---- Maintenance ----------------------------------------------------------------

export function MaintenanceSection() {
  const stats = useArchiveStats()
  const idx = stats.data?.index

  const rescan = useRescan()
  const refresh = () =>
    rescan.mutate(undefined, {
      onSuccess: (result) =>
        toast(
          `Rescanned: ${result.scanned} scanned, ${result.parsed} parsed, ${result.removed} removed.`,
        ),
      onError: (err) => toast(err.message, 'bad'),
    })

  return (
    <SettingsSection
      title="Maintenance"
      lede="The in-memory index behind every list and filter."
    >
      <div className="flex flex-wrap gap-x-8 gap-y-3 py-[13px]">
        <Stat
          value={idx ? idx.scanned.toLocaleString() : '—'}
          label="scanned"
        />
        <Stat value={idx ? idx.parsed.toLocaleString() : '—'} label="parsed" />
        <Stat
          value={idx ? idx.unchanged.toLocaleString() : '—'}
          label="unchanged"
        />
        <Stat
          value={idx ? idx.removed.toLocaleString() : '—'}
          label="removed"
        />
        <Stat value={idx ? `${idx.seconds}s` : '—'} label="last scan took" />
      </div>
      <OptionRow
        label="Rescan now"
        hint="The index is built when the server starts and every write in the app refreshes it — nothing rescans on a timer. This is for changes made outside the app (a card dropped in by hand, a make script). The same button sits in the top bar."
      >
        <button
          type="button"
          onClick={refresh}
          disabled={rescan.isPending}
          className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text disabled:opacity-60"
        >
          <RefreshCw
            className={cn('size-3.5', rescan.isPending && 'animate-spin')}
          />
          Rescan
        </button>
      </OptionRow>
    </SettingsSection>
  )
}

// ---- Media ----------------------------------------------------------------

/**
 * Media (docs/UI_REWRITE_PLAN.md Stage 6B C2).
 *
 * This section was dropped at Stage 6 on an incomplete reading: the
 * justification named the mock's three fixed-policy toggles (download on
 * import, images only, concurrent downloads) and missed its fourth row, a live
 * Rescan button. That row is a real archive-wide capability the old UI had as
 * "Bulk Localize All Characters", and dropping it silently is exactly what the
 * parity ledger now exists to prevent.
 *
 * So: the three toggles stay dropped, individually and for their own reason,
 * and the actions come back.
 */
export function MediaSection() {
  const active = useActiveBulkJob()
  const [startedId, setStartedId] = useState<string | null>(null)
  const jobId = startedId ?? active.data?.job_id ?? null
  const job = useMediaJob(jobId)
  const start = useBulkLocalize()
  const cancel = useCancelMediaJob()

  const live = job.data
  const running = live?.state === 'queued' || live?.state === 'running'

  const dedupe = useMutation({
    mutationFn: () =>
      unwrap(
        apiClient.POST('/api/v1/media/dedupe'),
        'could not clean up duplicate gallery files',
      ),
    onSuccess: (result) => {
      toast(
        result.files_trashed
          ? `Removed ${result.files_trashed.toLocaleString()} duplicate file${result.files_trashed === 1 ? '' : 's'} across ${result.folders_touched.toLocaleString()} folder${result.folders_touched === 1 ? '' : 's'}, freeing ${formatBytes(result.bytes_freed)}.`
          : 'No exact-duplicate gallery files found.',
      )
    },
    onError: (err) => toast(err.message, 'bad'),
  })

  const run = (skipComplete: boolean) =>
    start.mutate(
      { skipComplete },
      {
        onSuccess: (result) => {
          setStartedId(result.job_id)
          toast(
            skipComplete
              ? 'Localizing media for every card that needs it.'
              : 'Re-checking media for every card, including finished ones.',
          )
        },
        onError: (err) => toast(err.message, 'bad'),
      },
    )

  return (
    <SettingsSection
      title="Media"
      lede="Images referenced by creator notes, greetings and lorebook entries, downloaded into each card's gallery."
    >
      <CivitaiKeyRow />
      <div className="py-[13px]">
        <div className="text-[13.5px]">
          Localize media for the whole archive
        </div>
        <p className="mt-0.5 text-[11.5px] text-faint">
          Scans every card server-side and downloads what it finds. This runs on
          the server, so you can close this tab — but it does not survive a
          server restart; just start it again, since finished cards are skipped.
        </p>
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => run(true)}
            disabled={running || start.isPending}
            className="flex h-[31px] items-center gap-1.5 rounded-lg border border-sage-line bg-sage-dim px-3 text-[12.5px] text-sage hover:bg-sage-dim/70 disabled:opacity-50"
          >
            <Download className="size-3.5" />
            {running ? 'Running…' : 'Localize all'}
          </button>
          <button
            type="button"
            onClick={() => run(false)}
            disabled={running || start.isPending}
            title="Re-checks cards whose last run finished cleanly, including retrying URLs previously marked dead."
            className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text disabled:opacity-60"
          >
            <RefreshCw className="size-3.5" />
            Rescan everything
          </button>
          {running && jobId && (
            <button
              type="button"
              onClick={() =>
                cancel.mutate(jobId, {
                  onSuccess: () => toast('Stopping after the current card.'),
                  onError: (err) => toast(err.message, 'bad'),
                })
              }
              className="h-[31px] rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-bad/40 hover:text-bad"
            >
              Stop
            </button>
          )}
        </div>

        {live && (
          <div className="mt-3">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-raised">
              <div
                className="h-full rounded-full bg-sage transition-[width]"
                style={{
                  width: `${live.cards_total ? Math.round((live.done / live.cards_total) * 100) : 0}%`,
                }}
              />
            </div>
            <div className="mt-2 font-mono text-[11.5px] text-faint">
              {live.done.toLocaleString()} / {live.cards_total.toLocaleString()}{' '}
              cards · {live.cards_done.toLocaleString()} localized ·{' '}
              {live.cards_skipped.toLocaleString()} skipped ·{' '}
              {live.saved.toLocaleString()} files saved
              {live.errors > 0 && ` · ${live.errors.toLocaleString()} errors`}
              {live.state !== 'running' && live.state !== 'queued' && (
                <> · {live.state}</>
              )}
            </div>
            {live.current_card_id && (
              <div className="mt-1 truncate font-mono text-[11.5px] text-faint">
                {live.current_card_id}
              </div>
            )}
          </div>
        )}
      </div>

      <OptionRow
        label="Remove duplicate gallery files"
        hint="Trashes exact-byte duplicates left behind by a media re-download. Anything else is left alone."
      >
        <button
          type="button"
          onClick={() => dedupe.mutate()}
          disabled={dedupe.isPending}
          className="flex h-[31px] items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-muted hover:border-white/20 hover:text-text disabled:opacity-60"
        >
          <Trash2 className="size-3.5" />
          {dedupe.isPending ? 'Cleaning…' : 'Clean up duplicates'}
        </button>
      </OptionRow>

      <OptionRow
        label="Images only"
        hint="Audio and video are never persisted — enforced server-side at three points, not a per-run choice."
      />
      <OptionRow
        label="Concurrent downloads"
        hint="Fixed server policy: 6 at a time, 3 per host."
      />
    </SettingsSection>
  )
}
