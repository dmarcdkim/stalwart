/*
 * SPDX-FileCopyrightText: 2020 Stalwart Labs LLC <hello@stalw.art>
 *
 * SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEL
 */

use common::config::smtp::report::AggregateFrequency;
use mail_parser::DateTime;
use std::time::SystemTime;

pub mod analysis;
pub mod dkim;
pub mod dmarc;
pub mod inbound;
pub mod index;
pub mod scheduler;
pub mod send;
pub mod spf;
pub mod tls;

/// Spread applied to periodic reports, unchanged from upstream: up to three hours.
pub const DEFAULT_SEND_SPREAD_SECS: u64 = 3 * 60 * 60;

/// Upper bound of the randomized delay applied to a compiled aggregate report
/// before it is handed to the outbound queue, given the length of the window it
/// covers.
///
/// The delay exists to spread report bursts across receivers, which is only
/// coherent while several senders share a window. A sub-hourly window aggregates
/// too few senders for a burst to form, and is only ever configured because
/// somebody is waiting on the report, so no delay is applied there. Windows of an
/// hour or more keep the upstream three-hour spread unchanged.
pub fn send_spread_secs(window_secs: u64) -> u64 {
    if window_secs < 3600 {
        0
    } else {
        DEFAULT_SEND_SPREAD_SECS
    }
}

#[cfg(test)]
mod send_spread_tests {
    use super::{DEFAULT_SEND_SPREAD_SECS, send_spread_secs};

    #[test]
    fn sub_hourly_windows_are_not_spread() {
        // 0 is also what keeps `random_range(0..spread)` out of an empty range.
        assert_eq!(send_spread_secs(60), 0);
        assert_eq!(send_spread_secs(3599), 0);
        assert_eq!(send_spread_secs(3600), DEFAULT_SEND_SPREAD_SECS);
        assert_eq!(send_spread_secs(86400), DEFAULT_SEND_SPREAD_SECS);
    }
}

pub trait AggregateTimestamp {
    fn to_timestamp(&self) -> u64;
    fn to_timestamp_(&self, dt: DateTime) -> u64;
    fn as_secs(&self) -> u64;
    fn due(&self) -> u64;
}

impl AggregateTimestamp for AggregateFrequency {
    fn to_timestamp(&self) -> u64 {
        self.to_timestamp_(DateTime::from_timestamp(
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .map_or(0, |d| d.as_secs()) as i64,
        ))
    }

    fn to_timestamp_(&self, mut dt: DateTime) -> u64 {
        (match self {
            AggregateFrequency::Minutely => {
                dt.second = 0;
                dt.to_timestamp()
            }
            AggregateFrequency::Hourly => {
                dt.minute = 0;
                dt.second = 0;
                dt.to_timestamp()
            }
            AggregateFrequency::Daily => {
                dt.hour = 0;
                dt.minute = 0;
                dt.second = 0;
                dt.to_timestamp()
            }
            AggregateFrequency::Weekly => {
                let dow = dt.day_of_week();
                dt.hour = 0;
                dt.minute = 0;
                dt.second = 0;
                dt.to_timestamp() - (86400 * dow as i64)
            }
            AggregateFrequency::Never => dt.to_timestamp(),
        }) as u64
    }

    fn as_secs(&self) -> u64 {
        match self {
            AggregateFrequency::Minutely => 60,
            AggregateFrequency::Hourly => 3600,
            AggregateFrequency::Daily => 86400,
            AggregateFrequency::Weekly => 7 * 86400,
            AggregateFrequency::Never => 0,
        }
    }

    fn due(&self) -> u64 {
        self.to_timestamp() + self.as_secs()
    }
}
