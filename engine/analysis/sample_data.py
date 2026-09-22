"""固定演示样本（英文写作风格区分度足够的短文）。

既被 ``seed_samples`` / ``demo_analysis`` 管理命令使用，也被测试引用，
保证 README 中的“真实指标”可复现。刻意在 TARGET 中植入两处重复片段，
用于展示 repeated_fragments 指标。
"""

STUDENT_SAMPLES = [
    {
        "name": "student-01-my-hometown.txt",
        "text": (
            "My hometown is a small town near a wide river. I grew up there with my "
            "grandparents. The streets are narrow, but everyone knows each other. "
            "In the morning, vendors sell warm bread near the old bridge. Children "
            "walk to school in blue uniforms, laughing and chasing one another. "
            "When summer comes, we swim in the shallow water and catch little fish. "
            "I miss the slow life there, and I hope I can go back soon."
        ),
    },
    {
        "name": "student-02-weekend-market.txt",
        "text": (
            "Last Sunday I went to the weekend market with my mother. It was very "
            "crowded and noisy. We bought some apples, eggs, and a fresh fish. The "
            "fish was still jumping, which made me a little nervous. A kind old lady "
            "gave me a free orange because I helped her pick up her bag. On the way "
            "home, it suddenly rained, so we ran to a small shop and waited. I was "
            "wet but happy. I think these small days are the best part of my life."
        ),
    },
    {
        "name": "student-03-my-best-friend.txt",
        "text": (
            "My best friend is called Emma. We met on the first day of middle school. "
            "She sat next to me and shared her eraser without asking anything. Since "
            "then, we have done almost everything together. She is taller than me and "
            "she plays basketball really well. Sometimes we argue about stupid things, "
            "but we always say sorry before the day ends. I trust her more than anyone "
            "else. I hope we will still be close when we grow older."
        ),
    },
    {
        "name": "student-04-school-trip.txt",
        "text": (
            "Our class went to a science museum on Friday. The bus ride took about an "
            "hour. At the museum, we saw robots, old stones, and a model rocket. My "
            "favorite part was the space hall because I could stand inside a model of "
            "a spaceship. Our teacher asked us to write down three new facts we learned. "
            "I wrote that light from the sun takes eight minutes to reach us. After the "
            "trip, I felt tired but curious about the universe and want to learn more."
        ),
    },
]

REFERENCE_SAMPLES = [
    {
        "name": "reference-01-urban-transport.txt",
        "text": (
            "Urban transportation systems shape the daily experience of city residents. "
            "When networks are dense and reliable, workers can reach employment centers "
            "without depending on private vehicles. Reliable networks also reduce the "
            "cost of moving goods across metropolitan areas. Planners therefore tend to "
            "evaluate transit policy through accessibility rather than speed alone. "
            "Furthermore, the environmental consequences of modal choice have encouraged "
            "municipalities to invest in electrified corridors and protected bicycle lanes. "
            "Although such investments require sustained funding, evidence suggests that "
            "compact, walkable neighborhoods deliver measurable benefits to public health."
        ),
    },
    {
        "name": "reference-02-soil-microbes.txt",
        "text": (
            "Soil microorganisms regulate the rate at which organic matter decomposes and "
            "releases nutrients. Bacterial communities respond rapidly to changes in moisture "
            "and temperature, whereas fungal networks often stabilize aggregates over longer "
            "periods. Agricultural practices that minimize mechanical disturbance appear to "
            "preserve microbial diversity more effectively than intensive tillage. Moreover, "
            "cover cropping contributes organic residues that feed decomposer populations "
            "throughout the off-season. Researchers continue to examine how these below-ground "
            "processes interact with crop yields and atmospheric carbon exchange over time."
        ),
    },
    {
        "name": "reference-03-central-banks.txt",
        "text": (
            "Central banks influence economic activity primarily through monetary conditions. "
            "When inflation expectations rise, policymakers may tighten the supply of reserves, "
            "which tends to raise borrowing costs and dampen credit growth. Conversely, periods "
            "of weak demand often justify accommodative measures, although the transmission of "
            "such measures depends on the health of commercial bank balance sheets. Scholars "
            "additionally emphasize the importance of clear communication, because uncertainty "
            "about future rates can itself amplify market volatility across asset classes."
        ),
    },
]

# 待分析文本：学生口吻，并刻意含两处重复片段（含跨段复制粘贴）。
TARGET_TEXT = (
    "Last weekend, I visited a small village library with my classmates. "
    "The building was old, and the wooden floor made a soft sound under our feet. "
    "The librarian told us not to speak loudly, so everyone whispered. "
    "I chose a book about sea animals and sat near the window. "
    "The librarian told us not to speak loudly, so everyone whispered. "
    "Outside, the wind moved the leaves, and a cat slept on the warm stone step.\n\n"
    "After reading for an hour, we wrote short notes about our books. "
    "I learned that octopuses can change both color and texture very quickly. "
    "My friend Lily read a story about a brave dog and nearly cried. "
    "When the visit ended, we thanked the librarian and walked back together. "
    "It was a quiet day, but I felt strangely happy. "
    "I learned that octopuses can change both color and texture very quickly. "
    "I want to visit the library again next month and borrow another book."
)
TARGET_FILENAME = "target-student-library-visit.txt"
