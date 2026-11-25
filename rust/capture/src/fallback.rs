//! capture-analytics' emergency Kafka fallback. The idle fallback's topics
//! are checked on every boot, so a broken fallback shows up on an ordinary
//! deploy rather than in the emergency.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use metrics::gauge;
use tracing::{error, warn};

use crate::config::{CaptureMode, Config};
use crate::outputs::Target;
use crate::producers::ProducerName;
use crate::sinks::producer::KafkaProducer;
use crate::sinks::registry::{OutputTable, OutputTarget};

pub const ARMING_VAR: &str = "CAPTURE_ANALYTICS_KAFKA_EMERGENCY_FALLBACK";
pub const ARMING_VALUE: &str = "FALLBACK-TO-BACKUP-KAFKA-NOW";
pub const CHECK_VAR: &str = "CAPTURE_ANALYTICS_KAFKA_EMERGENCY_FALLBACK_CHECK";
pub const FALLBACK_BROKERS_VAR: &str = "KAFKA_FALLBACK_PRODUCER_METADATA_BROKER_LIST";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckMode {
    Warn,
    Strict,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FallbackSettings {
    pub armed: bool,
    pub check: CheckMode,
}

impl FallbackSettings {
    pub fn live(&self) -> Target {
        if self.armed {
            Target::Fallback
        } else {
            Target::Primary
        }
    }

    pub fn fallback_gates_liveness(&self) -> bool {
        !self.armed && self.check == CheckMode::Strict
    }

    pub fn from_config(config: &Config) -> anyhow::Result<Option<Self>> {
        let arming = config.kafka_emergency_fallback.as_deref();
        let check = config.kafka_emergency_fallback_check.as_deref();
        if config.capture_mode != CaptureMode::Events {
            if [arming, check]
                .iter()
                .any(|v| v.is_some_and(|v| !v.is_empty()))
            {
                warn!(
                    capture_mode = config.capture_mode.as_tag(),
                    "{ARMING_VAR} and {CHECK_VAR} apply only to CAPTURE_MODE=events; ignored"
                );
            }
            return Ok(None);
        }
        Ok(Some(Self {
            armed: parse_arming(arming)?,
            check: parse_check(check)?,
        }))
    }
}

/// Any other value refuses boot, so a typo is a crashloop an operator sees,
/// not a switch they think happened.
pub fn parse_arming(raw: Option<&str>) -> anyhow::Result<bool> {
    match raw {
        None | Some("") => Ok(false),
        Some(ARMING_VALUE) => Ok(true),
        Some(other) => anyhow::bail!(
            "invalid {ARMING_VAR}={other:?}: leave it unset or empty for normal operation, \
             or set it to exactly {ARMING_VALUE:?} to publish to the fallback cluster"
        ),
    }
}

pub fn parse_check(raw: Option<&str>) -> anyhow::Result<CheckMode> {
    match raw {
        None | Some("") | Some("warn") => Ok(CheckMode::Warn),
        Some("strict") => Ok(CheckMode::Strict),
        Some(other) => {
            anyhow::bail!(
                "invalid {CHECK_VAR}={other:?}: expected \"warn\" (default) or \"strict\""
            )
        }
    }
}

pub fn secondary_table(
    primary: &OutputTable,
    env: &HashMap<String, String>,
) -> anyhow::Result<OutputTable> {
    let read = |var: String| env.get(&var).filter(|v| !v.is_empty()).map(|v| (var, v));
    let producer = |var: String| -> anyhow::Result<ProducerName> {
        match read(var) {
            None => Ok(ProducerName::Fallback),
            Some((var, value)) => value
                .parse()
                .map_err(|e: anyhow::Error| anyhow::anyhow!("invalid {var}: {e}")),
        }
    };

    let mut configured = HashMap::new();
    for target in primary.analytics_family_targets() {
        let name = target.name;
        let topic = read(format!("CAPTURE_OUTPUT_{name}_SECONDARY_TOPIC"))
            .map(|(_, topic)| Arc::from(topic.as_str()));
        let producer = producer(format!("CAPTURE_OUTPUT_{name}_SECONDARY_PRODUCER"))?;
        configured.insert(name, (topic, producer));
    }
    let custom_producer = producer("CAPTURE_OUTPUT_CUSTOM_SECONDARY_PRODUCER".to_string())?;

    Ok(primary.map_targets(
        |target| {
            let (topic, producer) = configured
                .get(target.name)
                .cloned()
                .unwrap_or((None, ProducerName::Fallback));
            OutputTarget {
                name: target.name,
                topic: topic.unwrap_or_else(|| Arc::clone(&target.topic)),
                producer,
            }
        },
        custom_producer,
    ))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OnCheckFailure {
    Warn,
    RefuseBoot,
}

/// An idle primary only warns: arming means its cluster is degraded, and its
/// failure must not block the switch.
pub fn on_check_failure(target: Target, settings: FallbackSettings) -> OnCheckFailure {
    match (target, settings.armed, settings.check) {
        (Target::Fallback, true, _) | (Target::Fallback, false, CheckMode::Strict) => {
            OnCheckFailure::RefuseBoot
        }
        (Target::Fallback, false, CheckMode::Warn) | (Target::Primary, _, _) => {
            OnCheckFailure::Warn
        }
    }
}

pub const OUTPUT_TOPIC_READY: &str = "capture_kafka_output_topic_ready";

const TOPIC_CHECK_TIMEOUT: Duration = Duration::from_secs(10);

pub async fn run_boot_checks<P: KafkaProducer + 'static>(
    primary: &OutputTable<Arc<P>>,
    secondary: &OutputTable<Arc<P>>,
    settings: FallbackSettings,
) -> anyhow::Result<()> {
    if settings.armed {
        check_target(primary, Target::Primary, settings).await?;
    }
    check_target(secondary, Target::Fallback, settings).await
}

async fn check_target<P: KafkaProducer + 'static>(
    table: &OutputTable<Arc<P>>,
    target: Target,
    settings: FallbackSettings,
) -> anyhow::Result<()> {
    let failures = table
        .check_analytics_family_topics(TOPIC_CHECK_TIMEOUT)
        .await;
    for output in table.analytics_family_targets() {
        let output = output.name;
        let ready = !failures.iter().any(|failure| failure.output == output);
        gauge!(OUTPUT_TOPIC_READY, "target" => target.as_str(), "output" => output).set(if ready {
            1.0
        } else {
            0.0
        });
    }
    if failures.is_empty() {
        return Ok(());
    }

    let message = format!(
        "{} target failed its topic check: {}",
        target.as_str(),
        failures
            .iter()
            .map(|f| format!("output {} topic {:?}: {}", f.output, f.topic, f.error))
            .collect::<Vec<_>>()
            .join("; ")
    );
    match on_check_failure(target, settings) {
        OnCheckFailure::Warn => error!("{message}"),
        OnCheckFailure::RefuseBoot => anyhow::bail!(message),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ordering::OrderingGuarantee;
    use crate::outputs::{Output, PreparedEvent, PublishPrepared};
    use crate::pipeline::Address;
    use crate::sinks::kafka::KafkaSinkBase;
    use crate::sinks::producer::MockKafkaProducer;
    use crate::sinks::registry::test_outputs;
    use rstest::rstest;

    fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[rstest]
    #[case::unset(None, false)]
    #[case::empty(Some(""), false)]
    #[case::sentinel(Some(ARMING_VALUE), true)]
    fn arming_accepts_only_unset_empty_or_the_sentinel(
        #[case] raw: Option<&str>,
        #[case] armed: bool,
    ) {
        assert_eq!(parse_arming(raw).unwrap(), armed);
    }

    #[rstest]
    #[case("1")]
    #[case("true")]
    #[case("yes")]
    #[case(" ")]
    #[case(" FALLBACK-TO-BACKUP-KAFKA-NOW")]
    #[case("FALLBACK-TO-BACKUP-KAFKA-NOW\n")]
    #[case("fallback-to-backup-kafka-now")]
    #[case("FALLBACK-TO-BACKUP-KAFKA")]
    fn any_other_arming_value_refuses_boot(#[case] raw: &str) {
        let err = parse_arming(Some(raw)).unwrap_err().to_string();

        assert!(err.contains(ARMING_VAR), "{err}");
        assert!(err.contains(ARMING_VALUE), "{err}");
    }

    #[rstest]
    #[case(None, CheckMode::Warn)]
    #[case(Some(""), CheckMode::Warn)]
    #[case(Some("warn"), CheckMode::Warn)]
    #[case(Some("strict"), CheckMode::Strict)]
    fn check_mode_defaults_to_warn(#[case] raw: Option<&str>, #[case] mode: CheckMode) {
        assert_eq!(parse_check(raw).unwrap(), mode);
    }

    #[test]
    fn unknown_check_mode_refuses_boot() {
        let err = parse_check(Some("STRICT")).unwrap_err().to_string();
        assert!(err.contains(CHECK_VAR), "{err}");
    }

    fn config(mode: &str, arming: &str, check: &str) -> Config {
        envconfig::Envconfig::init_from_hashmap(&env(&[
            ("REDIS_URL", "redis://localhost:6379/"),
            ("CAPTURE_MODE", mode),
            (ARMING_VAR, arming),
            (CHECK_VAR, check),
        ]))
        .unwrap()
    }

    #[test]
    fn settings_read_their_variables_on_events_pods() {
        let settings = FallbackSettings::from_config(&config("events", ARMING_VALUE, "strict"))
            .unwrap()
            .unwrap();
        assert_eq!(settings.live(), Target::Fallback);
        assert_eq!(settings.check, CheckMode::Strict);

        assert!(FallbackSettings::from_config(&config("events", "true", "")).is_err());
        assert!(FallbackSettings::from_config(&config("events", "", "bogus")).is_err());
    }

    #[rstest]
    #[case("recordings")]
    #[case("ai")]
    #[case("import")]
    fn other_modes_never_parse_the_fallback_settings(#[case] mode: &str) {
        assert_eq!(
            FallbackSettings::from_config(&config(mode, "not-the-sentinel", "bogus")).unwrap(),
            None
        );
    }

    #[test]
    fn secondary_targets_default_to_the_primary_topic_on_the_fallback_producer() {
        let primary = test_outputs();
        let secondary = secondary_table(&primary, &HashMap::new()).unwrap();

        for (p, s) in primary
            .analytics_family_targets()
            .into_iter()
            .zip(secondary.analytics_family_targets())
        {
            assert_eq!((s.name, &s.topic), (p.name, &p.topic));
            assert_eq!(s.producer, ProducerName::Fallback, "{}", s.name);
        }
        assert_eq!(
            secondary.session_replay_main.topic,
            primary.session_replay_main.topic
        );
        assert!(secondary
            .producers()
            .iter()
            .all(|p| *p == ProducerName::Fallback));
        assert_eq!(
            secondary.has_ai_overflow_lane(),
            primary.has_ai_overflow_lane()
        );
    }

    #[test]
    fn secondary_targets_read_their_own_variables() {
        let secondary = secondary_table(
            &test_outputs(),
            &env(&[
                ("CAPTURE_OUTPUT_ANALYTICS_MAIN_SECONDARY_TOPIC", "ws_events"),
                ("CAPTURE_OUTPUT_DLQ_SECONDARY_TOPIC", "ws_dlq"),
                ("CAPTURE_OUTPUT_DLQ_SECONDARY_PRODUCER", "INGESTION"),
                ("CAPTURE_OUTPUT_AI_MAIN_SECONDARY_TOPIC", ""),
                ("CAPTURE_OUTPUT_HEATMAPS_SECONDARY_PRODUCER", ""),
                ("CAPTURE_OUTPUT_CUSTOM_SECONDARY_PRODUCER", "INGESTION"),
            ]),
        )
        .unwrap();

        assert_eq!(&*secondary.analytics_main.topic, "ws_events");
        assert_eq!(&*secondary.dlq.topic, "ws_dlq");
        assert_eq!(secondary.dlq.producer, ProducerName::Ingestion);
        assert_eq!(&*secondary.ai_main.topic, "ai_events", "empty means unset");
        assert_eq!(secondary.heatmaps.producer, ProducerName::Fallback);
        assert_eq!(secondary.custom_producer, ProducerName::Ingestion);
    }

    #[test]
    fn an_unknown_secondary_producer_names_its_variable() {
        let err = secondary_table(
            &test_outputs(),
            &env(&[(
                "CAPTURE_OUTPUT_AI_OVERFLOW_SECONDARY_PRODUCER",
                "WARPSTREAM",
            )]),
        )
        .unwrap_err()
        .to_string();

        assert!(
            err.contains("CAPTURE_OUTPUT_AI_OVERFLOW_SECONDARY_PRODUCER"),
            "{err}"
        );
    }

    #[rstest]
    #[case::idle_warn(Target::Fallback, false, CheckMode::Warn, OnCheckFailure::Warn)]
    #[case::idle_strict(Target::Fallback, false, CheckMode::Strict, OnCheckFailure::RefuseBoot)]
    #[case::armed_warn(Target::Fallback, true, CheckMode::Warn, OnCheckFailure::RefuseBoot)]
    #[case::armed_strict(Target::Fallback, true, CheckMode::Strict, OnCheckFailure::RefuseBoot)]
    #[case::idle_primary_warn(Target::Primary, true, CheckMode::Warn, OnCheckFailure::Warn)]
    #[case::idle_primary_strict(Target::Primary, true, CheckMode::Strict, OnCheckFailure::Warn)]
    fn check_failure_matrix(
        #[case] target: Target,
        #[case] armed: bool,
        #[case] check: CheckMode,
        #[case] expected: OnCheckFailure,
    ) {
        assert_eq!(
            on_check_failure(target, FallbackSettings { armed, check }),
            expected
        );
    }

    struct Missing<'a> {
        on_primary: &'a [&'a str],
        on_fallback: &'a [&'a str],
    }

    fn missing_on_fallback<'a>(topics: &'a [&'a str]) -> Missing<'a> {
        Missing {
            on_primary: &[],
            on_fallback: topics,
        }
    }

    async fn check(missing: Missing<'_>, armed: bool, check: CheckMode) -> anyhow::Result<()> {
        let cluster = |topics: &[&str]| {
            Arc::new(MockKafkaProducer::new().with_missing_topics(topics.iter().copied()))
        };
        let (primary_producer, fallback_producer) =
            (cluster(missing.on_primary), cluster(missing.on_fallback));
        let primary = test_outputs();
        let secondary = secondary_table(&primary, &HashMap::new())
            .unwrap()
            .map_producers(|_| Arc::clone(&fallback_producer));
        let primary = primary.map_producers(|_| Arc::clone(&primary_producer));
        run_boot_checks(&primary, &secondary, FallbackSettings { armed, check }).await
    }

    #[rstest]
    #[case::idle_warn(false, CheckMode::Warn, true)]
    #[case::idle_strict(false, CheckMode::Strict, false)]
    #[case::armed_warn(true, CheckMode::Warn, false)]
    #[case::armed_strict(true, CheckMode::Strict, false)]
    #[tokio::test]
    async fn a_missing_fallback_topic_warns_or_refuses_boot(
        #[case] armed: bool,
        #[case] mode: CheckMode,
        #[case] boots: bool,
    ) {
        let result = check(missing_on_fallback(&["heatmaps"]), armed, mode).await;

        assert_eq!(result.is_ok(), boots, "{result:?}");
        if let Err(err) = result {
            let err = err.to_string();
            assert!(
                err.contains("fallback") && err.contains("HEATMAPS"),
                "{err}"
            );
        }
    }

    #[rstest]
    #[case(false, CheckMode::Warn)]
    #[case(false, CheckMode::Strict)]
    #[case(true, CheckMode::Warn)]
    #[case(true, CheckMode::Strict)]
    #[tokio::test]
    async fn a_complete_fallback_passes_every_mode(#[case] armed: bool, #[case] mode: CheckMode) {
        check(missing_on_fallback(&[]), armed, mode).await.unwrap();
    }

    #[tokio::test]
    async fn a_missing_topic_on_the_idle_primary_does_not_block_the_switch() {
        let missing = Missing {
            on_primary: &["events_plugin_ingestion", "heatmaps"],
            on_fallback: &[],
        };

        check(missing, true, CheckMode::Strict).await.unwrap();
    }

    #[tokio::test]
    async fn the_topic_gauge_marks_only_the_failing_output() {
        let recorder = metrics_util::debugging::DebuggingRecorder::new();
        let snapshotter = recorder.snapshotter();
        let _guard = metrics::set_default_local_recorder(&recorder);

        check(missing_on_fallback(&["heatmaps"]), false, CheckMode::Warn)
            .await
            .unwrap();

        let mut not_ready: Vec<(String, f64)> = snapshotter
            .snapshot()
            .into_vec()
            .into_iter()
            .filter(|(key, ..)| key.key().name() == OUTPUT_TOPIC_READY)
            .map(|(key, _, _, value)| {
                let output = key
                    .key()
                    .labels()
                    .find(|label| label.key() == "output")
                    .unwrap()
                    .value()
                    .to_string();
                let metrics_util::debugging::DebugValue::Gauge(value) = value else {
                    panic!("{OUTPUT_TOPIC_READY} is a gauge");
                };
                (output, value.into_inner())
            })
            .filter(|(_, value)| *value == 0.0)
            .collect();
        not_ready.sort_by(|a, b| a.0.cmp(&b.0));

        assert_eq!(not_ready, vec![("HEATMAPS".to_string(), 0.0)]);
    }
    struct RouteCase {
        address: Address,
        primary_topic: &'static str,
        fallback_topic: &'static str,
    }

    fn analytics_family_routes() -> Vec<RouteCase> {
        use crate::pipeline::{AiLane, AnalyticsLane, BasicLane, PipelineLane};
        let route = |address, primary_topic, fallback_topic| RouteCase {
            address,
            primary_topic,
            fallback_topic,
        };
        vec![
            route(
                Address::Lane(PipelineLane::Analytics(AnalyticsLane::Main)),
                "events_plugin_ingestion",
                "ws_events",
            ),
            route(
                Address::Lane(PipelineLane::Analytics(AnalyticsLane::Overflow)),
                "events_plugin_ingestion_overflow",
                "events_plugin_ingestion_overflow",
            ),
            route(
                Address::Lane(PipelineLane::Analytics(AnalyticsLane::Historical)),
                "events_plugin_ingestion_historical",
                "events_plugin_ingestion_historical",
            ),
            route(
                Address::Lane(PipelineLane::Heatmaps(BasicLane::Main)),
                "heatmaps",
                "heatmaps",
            ),
            route(
                Address::Lane(PipelineLane::Warnings(BasicLane::Main)),
                "client_ingestion_warning",
                "client_ingestion_warning",
            ),
            route(
                Address::Lane(PipelineLane::ErrorTracking(BasicLane::Main)),
                "error_tracking_events",
                "error_tracking_events",
            ),
            route(
                Address::Lane(PipelineLane::Ai(AiLane::Main)),
                "ai_events",
                "ws_ai",
            ),
            route(
                Address::Lane(PipelineLane::Ai(AiLane::Overflow)),
                "ai_events_overflow",
                "ai_events_overflow",
            ),
            route(Address::Dlq, "events_plugin_ingestion_dlq", "ws_dlq"),
            route(
                Address::Custom("admin_topic".to_string()),
                "admin_topic",
                "admin_topic",
            ),
        ]
    }

    #[rstest]
    #[case::normal(Target::Primary)]
    #[case::armed(Target::Fallback)]
    #[tokio::test]
    async fn every_address_publishes_to_the_live_targets_topic(#[case] live: Target) {
        let primary_table = test_outputs();
        let secondary_table = secondary_table(
            &primary_table,
            &env(&[
                ("CAPTURE_OUTPUT_ANALYTICS_MAIN_SECONDARY_TOPIC", "ws_events"),
                ("CAPTURE_OUTPUT_AI_MAIN_SECONDARY_TOPIC", "ws_ai"),
                ("CAPTURE_OUTPUT_DLQ_SECONDARY_TOPIC", "ws_dlq"),
            ]),
        )
        .unwrap();
        let (primary, fallback) = (MockKafkaProducer::new(), MockKafkaProducer::new());
        let output = Output::select(
            Output::single(KafkaSinkBase::with_producer(primary.clone(), primary_table)),
            Output::single(KafkaSinkBase::with_producer(
                fallback.clone(),
                secondary_table,
            )),
            live,
        );
        let cases = analytics_family_routes();
        let events = cases
            .iter()
            .map(|case| PreparedEvent {
                uuid: uuid::Uuid::now_v7(),
                address: case.address.clone(),
                partition_key: "token:user".to_string(),
                ordering: OrderingGuarantee::PerDistinctId,
                payload: bytes::Bytes::from_static(b"{}"),
                headers: common_types::CapturedEventHeaders {
                    token: Some("token".to_string()),
                    distinct_id: Some("user".to_string()),
                    session_id: None,
                    timestamp: None,
                    event: Some("event".to_string()),
                    uuid: None,
                    now: None,
                    force_disable_person_processing: None,
                    historical_migration: None,
                    skip_heatmap_processing: None,
                    dlq_reason: None,
                    dlq_step: None,
                    dlq_timestamp: None,
                    content_encoding: None,
                    legacy_property_splice: None,
                },
            })
            .collect();

        output.publish_prepared(events).await;

        let (live_producer, idle_producer) = match live {
            Target::Primary => (&primary, &fallback),
            Target::Fallback => (&fallback, &primary),
        };
        let topics: Vec<String> = live_producer
            .get_records()
            .iter()
            .map(|record| record.topic.to_string())
            .collect();
        let expected: Vec<&str> = cases
            .iter()
            .map(|case| match live {
                Target::Primary => case.primary_topic,
                Target::Fallback => case.fallback_topic,
            })
            .collect();
        assert_eq!(topics, expected);
        assert!(idle_producer.get_records().is_empty());
    }
}
