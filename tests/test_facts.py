"""LoRa facts, the radio's own settings, and local facts in the system prompt."""

from bot.personas import LORA_FACTS, radio_facts
from bot.prompt import build_system_prompt
from bot.service import Decision


def test_radio_facts_line():
    info = {"radio_freq": 910.525, "radio_bw": 62.5, "radio_sf": 7, "radio_cr": 5, "tx_power": 22}
    assert radio_facts(info) == "This radio is set to 910.525 MHz, 62.5 kHz bandwidth, SF7, coding rate 4/5, 22 dBm."
    assert radio_facts({}) == ""
    assert radio_facts({"radio_sf": 9}) == "This radio is set to SF9."


def test_lora_facts_disambiguate_coding_rate():
    assert "4/8 has the most error correction" in LORA_FACTS
    assert "'higher coding rate' means more error correction" in LORA_FACTS


def test_system_prompt_carries_facts_after_the_rules():
    p = build_system_prompt("MeshAI", 100, "Voice: dry.", facts="One fact.  Two   facts.")
    assert p.endswith("One fact. Two facts.")
    assert "(9)" in p and p.index("(9)") < p.index("One fact.")
    assert build_system_prompt("MeshAI", 100, "Voice: dry.") == build_system_prompt("MeshAI", 100, "Voice: dry.", facts="   ")


async def test_prompt_includes_lora_facts_radio_settings_and_config_facts(harness):
    h = harness(facts="The mesh is centered on Madison, Wisconsin.")
    await h.service.start()  # reads the radio's self info
    assert await h.say("Alice: what coding rate are you on") is Decision.ANSWERED
    system = h.backend.calls[-1][0]["content"]
    assert LORA_FACTS in system
    assert "This radio is set to 910.525 MHz, 62.5 kHz bandwidth, SF7, coding rate 4/5, 22 dBm." in system
    assert system.endswith("The mesh is centered on Madison, Wisconsin.")


async def test_facts_survive_a_persona_switch(harness):
    h = harness(global_burst=5, sender_burst=5)
    await h.service.start()
    await h.say("Alice: /marvin")
    await h.say("Alice: hello, what does SF7 mean")
    system = h.backend.calls[-1][0]["content"]
    assert "cosmic gloom" in system and LORA_FACTS in system


async def test_facts_are_present_before_start_without_radio_settings(harness):
    h = harness()
    assert await h.say("Alice: which coding rate is best") is Decision.ANSWERED
    system = h.backend.calls[-1][0]["content"]
    assert LORA_FACTS in system and "This radio is set to" not in system


async def test_radio_facts_only_for_radio_questions(harness):
    h = harness(facts="The mesh is centered on Madison, Wisconsin.", global_burst=9, sender_burst=9)
    await h.service.start()
    await h.say("Alice: any tips for keeping tomatoes alive in September?")
    system = h.backend.calls[-1][0]["content"]
    assert LORA_FACTS not in system and "This radio is set to" not in system
    assert "How this bot works" in system and system.endswith("The mesh is centered on Madison, Wisconsin.")
    for prompt in ("how far can SF7 reach", "is my antenna any good", "what is the noise floor tonight", "sf12 or sf7",
                   "What settings are you using?", "what are you running on"):
        await h.say(f"Bob: {prompt}")
        assert LORA_FACTS in h.backend.calls[-1][0]["content"], prompt
    for prompt in ("Have you heard any good jokes?", "what is the best route to the lake", "is the power out on your side of town"):
        await h.say(f"Carol: {prompt}")
        assert LORA_FACTS not in h.backend.calls[-1][0]["content"], prompt
