import { QueryClient } from '@tanstack/react-query'

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 1000,
        // This API can be on localhost even when the browser reports no Internet.
        networkMode: 'always', refetchOnReconnect: true,
        retry: (count, error) => count < 1 && (!error.status || error.status >= 500),
      },
      mutations: { networkMode: 'always', retry: false },
    },
  })
}
