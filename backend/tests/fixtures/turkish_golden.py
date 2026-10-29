"""Golden Turkish fixtures for Stage 5 validator, agent, and subtitle translation tests."""

from __future__ import annotations

GOLDEN_TURKISH_TITLE = (
    "Sadece 1 Kaşık! Çiçeksiz Orkidenin Tüm Yıl Çiçek Açmasını Sağlar | HayataDairPüfNoktalar"
)

GOLDEN_TURKISH_DESCRIPTION = """\
Merhaba arkadaşlar, HayataDairPüfNoktalar (@orchidhacks) kanalımıza hoş geldiniz!
Bugün sadece 1 kaşık tarçın ve 500 ml ılık su ile orkide bakımını anlatıyorum.

📌 Kanalımıza abone olun: https://www.youtube.com/@orchidhacks
📸 Instagram: https://www.instagram.com/orchidhacks

Malzemeler:
- 1 tatlı kaşığı toz tarçın
- 500 ml dinlendirilmiş su
- 2 damla limon suyu

#orkide #orkidebakımı #çiçekcoşturan #HayataDairPüfNoktalar
"""

GOLDEN_TURKISH_SRT = """\
1
00:00:01,000 --> 00:00:04,500
Merhaba arkadaşlar, HayataDairPüfNoktalar kanalına hoş geldiniz!

2
00:00:04,800 --> 00:00:09,200
Bugün sadece 1 kaşık toz tarçın ile orkide köklerini güçlendireceğiz.

3
00:00:09,500 --> 00:00:14,000
500 ml ılık suya karıştırıp ayda 1 kez uygulayabilirsiniz.
"""

# Simulates the live Stage 3b quirk observed on `captions.download?tfmt=srt&tlang=en`
# where cue 2 has an internal blank line before its text and cue 3 was left empty
# when Google Translate merged cue 2 and cue 3.
GOLDEN_YOUTUBE_TLANG_EN_QUIRK_SRT = """\
1
00:00:01,000 --> 00:00:04,500
Hello friends, welcome to the HayataDairPüfNoktalar channel!

2
00:00:04,800 --> 00:00:09,200

Today we will strengthen orchid roots with just 1 spoon of ground cinnamon in 500 ml water.

3
00:00:09,500 --> 00:00:14,000

"""
