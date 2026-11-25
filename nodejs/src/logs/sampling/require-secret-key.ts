import { AUTH_TYPE_SECRET_KEY } from '../secret-key-teams'
import type { LogsIngestionMessage } from '../types'
import type { SamplingRulesCache } from './sampling-rules-cache'

export type RequireSecretKeyDrops = Map<number, { messages: number; records: number; bytes: number; ruleId: string }>

export async function findRequireSecretKeyDrops<T extends LogsIngestionMessage>(
    messages: T[],
    cache: Pick<SamplingRulesCache, 'getCompiledRuleSet'>,
    rulesEnabledForTeam: (teamId: number) => boolean
): Promise<{ dropped: Set<T>; byTeam: RequireSecretKeyDrops }> {
    // Signed internal logs carry the public token, and the team's own senders did not send them.
    const candidates = messages.filter(
        (message) =>
            message.authType !== AUTH_TYPE_SECRET_KEY &&
            message.verifiedAsInternalLogs === undefined &&
            rulesEnabledForTeam(message.teamId)
    )
    const teamIds = [...new Set(candidates.map((message) => message.teamId))]
    const ruleIdByTeam = new Map(
        await Promise.all(
            teamIds.map(
                async (teamId) =>
                    [teamId, (await cache.getCompiledRuleSet(teamId, 'logs')).requireSecretKeyRuleId] as const
            )
        )
    )

    const dropped = new Set<T>()
    const byTeam: RequireSecretKeyDrops = new Map()
    for (const message of candidates) {
        const ruleId = ruleIdByTeam.get(message.teamId)
        if (!ruleId) {
            continue
        }
        dropped.add(message)
        const totals = byTeam.get(message.teamId) ?? { messages: 0, records: 0, bytes: 0, ruleId }
        totals.messages++
        totals.records += message.recordCount
        totals.bytes += message.bytesUncompressed
        byTeam.set(message.teamId, totals)
    }
    return { dropped, byTeam }
}
