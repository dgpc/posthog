import { afterEach, describe, expect, it, vi } from 'vitest'

import { handleAuthorize } from '@/handlers/authorize'
import { handleCallback } from '@/handlers/callback'
import worker from '@/index'
import { putClientMapping } from '@/lib/kv'

import { createInMemoryKV } from './helpers'

afterEach(() => {
    vi.unstubAllGlobals()
})

async function storeClient(kv: KVNamespace, proxyClientId: string, redirectUri: string): Promise<void> {
    await putClientMapping(kv, proxyClientId, {
        us_client_id: `${proxyClientId}_us`,
        eu_client_id: `${proxyClientId}_eu`,
        redirect_uris: [redirectUri],
        created_at: Date.now(),
    })
}

async function startFlow(kv: KVNamespace, proxyClientId: string, redirectUri: string, state: string): Promise<URL> {
    const request = new Request(
        `https://oauth.posthog.com/oauth/authorize/?client_id=${proxyClientId}&redirect_uri=${redirectUri}&response_type=code&state=${state}&_region=us`
    )
    const response = await handleAuthorize(request, kv)
    return new URL(response.headers.get('location')!)
}

describe('callback client binding', () => {
    it("does not let a second client's flow steal the first client's authorization code", async () => {
        const kv = createInMemoryKV()
        await storeClient(kv, 'clientA', 'https://a.example/cb')
        await storeClient(kv, 'clientB', 'https://b.example/cb')

        const regionalA = await startFlow(kv, 'clientA', 'https://a.example/cb', 'shared')
        await startFlow(kv, 'clientB', 'https://b.example/cb', 'shared')
        const callbackRequest = new Request(
            `${regionalA.searchParams.get('redirect_uri')}?code=victim_auth_code&state=${regionalA.searchParams.get('state')}`
        )
        const callbackResponse = await handleCallback(callbackRequest, kv)

        expect(callbackResponse.status).toBe(302)
        const location = new URL(callbackResponse.headers.get('location')!)
        expect(location.origin + location.pathname).toBe('https://a.example/cb')
        expect(location.searchParams.get('code')).toBe('victim_auth_code')
        expect(location.searchParams.get('state')).toBe('shared')
    })

    it.each([
        ["the other client's callback path", 'https://oauth.posthog.com/oauth/callback/clientB_us/'],
        ['the shared callback path', 'https://oauth.posthog.com/oauth/callback/'],
    ])("refuses a code issued to another client against a flow's nonce on %s", async (_name, callbackUrl) => {
        const kv = createInMemoryKV()
        await storeClient(kv, 'clientA', 'https://a.example/cb')
        await storeClient(kv, 'clientB', 'https://b.example/cb')

        const regionalA = await startFlow(kv, 'clientA', 'https://a.example/cb', 'a_state')
        const callbackRequest = new Request(
            `${callbackUrl}?code=code_for_client_b&state=${regionalA.searchParams.get('state')}`
        )
        const response = await worker.fetch(callbackRequest, { AUTH_KV: kv })

        expect(response.status).toBe(400)
        expect(response.headers.get('location')).toBeNull()
    })

    it.each([
        ['registered through the proxy', true],
        ['registered before callbacks were bound to clients', false],
    ])('completes the flow for a client %s', async (_name, registerThroughProxy) => {
        const kv = createInMemoryKV()
        const env = { AUTH_KV: kv }
        const regionalRequests: { url: string; body: string }[] = []
        vi.stubGlobal(
            'fetch',
            vi.fn(async (url: string, init: RequestInit) => {
                regionalRequests.push({ url: String(url), body: String(init.body) })
                const region = new URL(url).hostname.split('.')[0]
                const payload = url.endsWith('/oauth/register/')
                    ? { client_id: `clientC_${region}` }
                    : { access_token: 'pha_token', token_type: 'bearer' }
                return new Response(JSON.stringify(payload), {
                    status: url.endsWith('/oauth/register/') ? 201 : 200,
                    headers: { 'Content-Type': 'application/json' },
                })
            })
        )
        if (registerThroughProxy) {
            await worker.fetch(
                new Request('https://oauth.posthog.com/oauth/register/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ client_name: 'C', redirect_uris: ['https://c.example/cb'] }),
                }),
                env
            )
        } else {
            await putClientMapping(kv, 'clientC_us', {
                us_client_id: 'clientC_us',
                eu_client_id: 'clientC_eu',
                redirect_uris: ['https://c.example/cb'],
                created_at: Date.now(),
            })
        }

        const regional = await startFlow(kv, 'clientC_us', 'https://c.example/cb', 'c_state')
        const callback = await worker.fetch(
            new Request(
                `${regional.searchParams.get('redirect_uri')}?code=code_for_c&state=${regional.searchParams.get('state')}`
            ),
            env
        )
        const token = await worker.fetch(
            new Request('https://oauth.posthog.com/oauth/token/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: 'grant_type=authorization_code&code=code_for_c&client_id=clientC_us&redirect_uri=https%3A%2F%2Fc.example%2Fcb&code_verifier=v',
            }),
            env
        )

        expect(regional.searchParams.get('redirect_uri')).toBe('https://oauth.posthog.com/oauth/callback/clientC_us/')
        expect(callback.status).toBe(302)
        const location = new URL(callback.headers.get('location')!)
        expect(location.origin + location.pathname).toBe('https://c.example/cb')
        expect(location.searchParams.get('code')).toBe('code_for_c')
        expect(location.searchParams.get('state')).toBe('c_state')
        expect(token.status).toBe(200)
        const tokenRequest = regionalRequests.find(({ url }) => url.endsWith('/oauth/token/'))!
        expect(new URLSearchParams(tokenRequest.body).get('redirect_uri')).toBe(
            'https://oauth.posthog.com/oauth/callback/clientC_us/'
        )
    })
})
