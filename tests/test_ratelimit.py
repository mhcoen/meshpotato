import pytest

from bot.ratelimit import RateLimiter, TokenBucket


def test_bucket_starts_full_and_refills_over_time(clock):
    bucket = TokenBucket(rate_per_sec=1 / 60, capacity=1, clock=clock)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    clock.advance(30)
    assert bucket.try_acquire() is False
    clock.advance(30)
    assert bucket.try_acquire() is True


def test_bucket_never_exceeds_capacity(clock):
    bucket = TokenBucket(rate_per_sec=1.0, capacity=2, clock=clock)
    clock.advance(1000)
    assert bucket.peek() == 2.0


def test_bucket_rejects_bad_parameters(clock):
    with pytest.raises(ValueError):
        TokenBucket(0, 1, clock)
    with pytest.raises(ValueError):
        TokenBucket(1, 0, clock)


def test_global_limit_applies_across_senders(clock):
    rl = RateLimiter(global_per_min=1, global_burst=1, sender_per_min=60, sender_burst=10, clock=clock)
    assert rl.allow("alice").allowed
    d = rl.allow("bob")
    assert (d.allowed, d.reason) == (False, "global")
    clock.advance(60)
    assert rl.allow("bob").allowed


def test_sender_limit_is_per_name(clock):
    rl = RateLimiter(global_per_min=60, global_burst=10, sender_per_min=1, sender_burst=1, clock=clock)
    assert rl.allow("alice").allowed
    d = rl.allow("alice")
    assert (d.allowed, d.reason) == (False, "sender")
    assert rl.allow("bob").allowed  # a different (spoofable) name has its own bucket
    clock.advance(60)
    assert rl.allow("alice").allowed


def test_denied_request_consumes_nothing(clock):
    rl = RateLimiter(global_per_min=60, global_burst=3, sender_per_min=1, sender_burst=1, clock=clock)
    assert rl.allow("alice").allowed
    before = rl.snapshot()["global_tokens"]
    assert not rl.allow("alice").allowed  # sender exhausted, global still has tokens
    assert rl.snapshot()["global_tokens"] == before


def test_sender_table_is_bounded(clock):
    rl = RateLimiter(60, 10, 60, 10, clock=clock, max_senders=3)
    for name in ("a", "b", "c", "d"):
        rl.allow(name)
    assert set(rl.snapshot(recent=10)["senders"]) == {"b", "c", "d"}


def test_set_rate_zero_pauses_refill_and_keeps_held_tokens(clock):
    bucket = TokenBucket(rate_per_sec=1.0, capacity=2, clock=clock)
    assert bucket.try_acquire()
    bucket.set_rate(0.0)
    clock.advance(1000)
    assert bucket.peek() == 1.0  # the token already held survives; nothing new accrues
    bucket.set_rate(1.0)
    clock.advance(1)
    assert bucket.peek() == 2.0  # refill resumes from the moment the rate was restored


def test_set_rate_rejects_negative(clock):
    with pytest.raises(ValueError):
        TokenBucket(1.0, 1, clock).set_rate(-1)


def test_global_factor_scales_only_the_global_bucket(clock):
    rl = RateLimiter(global_per_min=2, global_burst=1, sender_per_min=60, sender_burst=1, clock=clock)
    assert rl.allow("alice").allowed
    rl.set_global_factor(0.5)  # 1/min effective
    clock.advance(30)
    assert not rl.allow("alice").allowed
    clock.advance(30)
    assert rl.allow("alice").allowed
    rl.set_global_factor(0.0)
    clock.advance(3600)
    d = rl.allow("bob")
    assert (d.allowed, d.reason) == (False, "global")
    rl.set_global_factor(1.0)
    clock.advance(30)
    assert rl.allow("bob").allowed
    assert rl.snapshot()["global_per_min"] == 2.0


def test_global_factor_bounds(clock):
    rl = RateLimiter(2, 1, 2, 1, clock=clock)
    with pytest.raises(ValueError):
        rl.set_global_factor(1.5)


def test_snapshot_reports_state_for_the_ui(clock):
    rl = RateLimiter(global_per_min=1, global_burst=1, sender_per_min=1, sender_burst=1, clock=clock)
    rl.allow("alice")
    snap = rl.snapshot()
    assert snap["global_tokens"] == 0.0
    assert snap["global_capacity"] == 1
    assert snap["senders"] == {"alice": 0.0}


@pytest.mark.parametrize('count', [0, 6, True, 1.5])
def test_report_burst_is_bounded(clock, count):
    limiter = RateLimiter(4, 1, 4, 1, clock=clock)
    first = limiter.reserve('Alice')
    with pytest.raises(ValueError):
        limiter.report_burst(first, count)
    first.refund()


def test_report_burst_requires_normal_admission_and_cannot_be_reused(clock):
    limiter = RateLimiter(4, 1, 4, 1, clock=clock)
    first = limiter.reserve('Alice')
    burst = limiter.report_burst(first, 5)
    assert not burst.reserve_next().allowed
    with pytest.raises(ValueError):
        limiter.report_burst(first, 5)
    with pytest.raises(ValueError):
        limiter.report_burst(limiter.reserve('Bob'), 5)
    first.commit()
    for _ in range(4):
        clock.advance(2)
        reservation = burst.reserve_next()
        assert reservation.allowed
        reservation.commit()
    assert not burst.reserve_next().allowed
    assert limiter.snapshot()['global_tokens'] == pytest.approx(0.53, abs=0.01)
    assert not limiter.reserve('Bob').allowed
    clock.advance(6)
    assert not limiter.reserve('Alice').allowed
    clock.advance(2)
    assert limiter.reserve('Alice').allowed


def test_report_burst_refunds_unsent_message_and_preserves_congestion_pause(clock):
    limiter = RateLimiter(4, 1, 4, 1, clock=clock)
    first = limiter.reserve('Alice')
    burst = limiter.report_burst(first, 5)
    first.commit()
    limiter.set_global_factor(0)
    clock.advance(20)
    assert not burst.reserve_next().allowed
    assert burst.remaining == 4
    limiter.set_global_factor(1)
    unsent = burst.reserve_next()
    assert limiter.snapshot()['global_tokens'] == 0
    unsent.refund()
    assert limiter.snapshot()['global_tokens'] == 0
    assert not limiter.reserve('Bob').allowed


def test_batch_allowance_refills_more_slowly_under_congestion(clock):
    limiter = RateLimiter(4, 1, 4, 1, clock=clock)
    first = limiter.reserve('Alice')
    burst = limiter.report_burst(first, 2)
    first.commit()
    burst.reserve_next().commit()
    limiter.set_global_factor(0.5)
    clock.advance(29)
    assert not limiter.reserve('Bob').allowed
    clock.advance(2)
    assert limiter.reserve('Bob').allowed


def test_refunded_first_message_does_not_authorize_followups(clock):
    limiter = RateLimiter(4, 1, 4, 1, clock=clock)
    first = limiter.reserve('Alice')
    burst = limiter.report_burst(first, 5)
    first.refund()
    assert not burst.reserve_next().allowed
    assert limiter.snapshot()['global_tokens'] == 1
