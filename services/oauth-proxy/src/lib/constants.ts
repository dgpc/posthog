export type Region = 'us' | 'eu'

export const POSTHOG_US_BASE_URL = 'https://us.posthog.com'
export const POSTHOG_EU_BASE_URL = 'https://eu.posthog.com'

export const REGION_BASE_URLS: Record<Region, string> = {
    us: POSTHOG_US_BASE_URL,
    eu: POSTHOG_EU_BASE_URL,
}

export function toRegion(value: string | undefined | null): Region {
    return value?.toLowerCase() === 'eu' ? 'eu' : 'us'
}

export function baseUrlForRegion(region: Region): string {
    return REGION_BASE_URLS[region]
}

export function regionForBaseUrl(baseUrl: string): Region | null {
    const match = Object.entries(REGION_BASE_URLS).find(([, url]) => url === baseUrl)
    return match ? (match[0] as Region) : null
}

export function proxyOrigin(request: Request): string {
    return new URL(request.url).origin
}

export const CALLBACK_PATH = '/oauth/callback/'

/**
 * The regional server accepts this path only for the application whose client_id it names
 * (`OAUTH_PROXY_CALLBACK_URLS` in Django), so the path tells the callback which client a code was issued to.
 */
export function clientCallbackUrl(origin: string, regionalClientId: string): string {
    return `${origin}${CALLBACK_PATH}${encodeURIComponent(regionalClientId)}/`
}

export function callbackPathClientId(pathname: string): string | null {
    if (!pathname.startsWith(CALLBACK_PATH)) {
        return null
    }
    const [segment, ...rest] = pathname.slice(CALLBACK_PATH.length).split('/')
    if (!segment || rest.some((part) => part !== '')) {
        return null
    }
    return segment
}
