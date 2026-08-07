import os
from modules.image_generator import ImageGenerator

print("Testing with EMPTY cookie...")

gen = ImageGenerator(output_dir="assets/generated_images", bing_cookie="")
print("Generating image...")
result = gen.generate(headline="Test Headline", category="tech_ai")

if result:
    print(f"SUCCESS! Image saved to {result}")
else:
    print("FAILED! generate() returned None")
