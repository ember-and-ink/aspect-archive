import { Navigate, useNavigate, useParams } from 'react-router'
import { ArrowLeft } from 'lucide-react'
import {
  ArchiveSection,
  LibrarySection,
  MaintenanceSection,
  MediaSection,
  ProvidersSection,
  SecuritySection,
  UserscriptsSection,
} from '@/components/settings/sections'
import { SettingsNav } from '@/components/settings/controls'
import {
  SETTINGS_SECTIONS,
  type SectionKey,
} from '@/components/settings/sections-def'

const SECTIONS: Record<SectionKey, React.ComponentType> = {
  library: LibrarySection,
  archive: ArchiveSection,
  providers: ProvidersSection,
  media: MediaSection,
  userscripts: UserscriptsSection,
  security: SecuritySection,
  maintenance: MaintenanceSection,
}

/**
 * Settings (docs/UI_REWRITE_PLAN.md §4.5, Stage 6) — no server changes: every
 * section reads/writes the same `/api/v1/settings`, `/stats`, `/proxy/status`,
 * `/userscripts` and `/refresh` routes that already existed. **Security** is
 * the one exception, added later with routes of its own (`/api/v1/security`).
 *
 * Dropped from the mock's seven-item nav: **Media** (its rows — "download on
 * import", "images only", "concurrent downloads" — describe fixed server
 * policy with no per-request knob behind them; see the notes in
 * `ArchiveSection` for the same call made about avatar compression) and
 * **About** (nothing in the API tracks a version to show). Both would have
 * been controls wired to nothing, which is the thing this rewrite's salvage
 * rule (docs/UI_REWRITE_PLAN.md §0) exists to avoid. **NSFW blur**, the plan's
 * open question 5, is resolved the same way: dropped, not built — there is no
 * NSFW signal in the API to key it off.
 */
export function SettingsPage() {
  const { section } = useParams<{ section?: string }>()
  const navigate = useNavigate()

  if (!section || !(section in SECTIONS)) {
    return <Navigate to={`/settings/${SETTINGS_SECTIONS[0].key}`} replace />
  }
  const Section = SECTIONS[section as SectionKey]

  return (
    <div className="mx-auto max-w-[980px] px-5 pb-24">
      <div className="sticky top-0 z-20 flex items-center gap-2.5 border-b border-line-soft bg-ground py-[11px]">
        <button
          type="button"
          onClick={() => navigate('/')}
          className="flex h-8 items-center gap-2 rounded-[10px] border border-line px-3 text-[13px] hover:bg-raised"
        >
          <ArrowLeft className="size-4" /> Back
        </button>
        <span className="text-[12.5px] text-faint">Settings</span>
        <span className="ml-auto text-[12.5px] text-faint">
          Saved automatically
        </span>
      </div>

      <div className="flex flex-col gap-8 pt-6 md:flex-row md:gap-10">
        <SettingsNav />
        <div className="flex min-w-0 flex-1 flex-col gap-9">
          <Section />
        </div>
      </div>
    </div>
  )
}
