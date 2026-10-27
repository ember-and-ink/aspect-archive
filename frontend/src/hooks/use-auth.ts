import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'
import type { components } from '@/lib/api-schema'

export type Session = components['schemas']['SessionOut']
export type SecuritySettings = components['schemas']['SecurityOut']
export type SecurityUpdate = components['schemas']['SecurityIn']

/**
 * Where this browser stands with the server's login gate
 * (`aspect/api/gate.py`). One of the three API routes answerable without a
 * login, which is what lets `AuthGate` ask before anything else is fetched.
 */
export function useSession() {
  return useQuery({
    queryKey: ['auth-session'],
    // Re-asked when a request comes back 401 (see `AuthGate`), not on a timer:
    // a session lasts a month and nothing else ends one behind the app's back.
    staleTime: Infinity,
    retry: false,
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/auth/session'),
        'could not check the login',
      ),
  })
}

export function useLogin() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { username: string; password: string }) =>
      unwrap(apiClient.POST('/api/v1/auth/login', { body }), 'login failed'),
    onSuccess: (session) => {
      qc.setQueryData(['auth-session'], session)
      // Anything fetched while locked out is cached as a 401.
      void qc.invalidateQueries({
        predicate: (query) => query.queryKey[0] !== 'auth-session',
      })
    },
  })
}

export function useLogout() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const { response } = await apiClient.POST('/api/v1/auth/logout')
      if (!response.ok) throw new Error(`could not log out: ${response.status}`)
    },
    onSuccess: () => {
      // Dropped rather than invalidated, so nothing the session could read
      // stays on screen or in memory behind the login.
      qc.clear()
    },
  })
}

/** The gate's own settings, for Settings → Security. */
export function useSecurity() {
  return useQuery({
    queryKey: ['security'],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/security'),
        'could not read the security settings',
      ),
  })
}

/**
 * Issue (or, with `revoke`, drop) the API token the userscripts send in place
 * of a login. Leaves the gate's other settings and this browser's session alone.
 */
export function useApiToken() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ revoke = false }: { revoke?: boolean } = {}) =>
      unwrap(
        revoke
          ? apiClient.DELETE('/api/v1/security/token')
          : apiClient.POST('/api/v1/security/token'),
        'could not change the API token',
      ),
    onSuccess: (saved) => qc.setQueryData(['security'], saved),
  })
}

export function useUpdateSecurity() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SecurityUpdate) =>
      unwrap(
        apiClient.PUT('/api/v1/security', { body }),
        'could not save the security settings',
      ),
    onSuccess: (saved) => {
      qc.setQueryData(['security'], saved)
      // The server hands the saving browser a session under the new
      // credentials, so it stays in.
      qc.setQueryData(['auth-session'], {
        enabled: saved.enabled,
        authenticated: true,
      } satisfies Session)
    },
  })
}
