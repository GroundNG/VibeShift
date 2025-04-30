from PIL import Image, ImageDraw, ImageFont
import io
import logging
import base64
from typing import Optional
import os

from ..llm.llm_client import LLMClient

logger = logging.getLogger(__name__)



# Helper Function
def stitch_images(img1: Image.Image, img2: Image.Image, label1="Baseline", label2="Current") -> Optional[Image.Image]:
        """Stitches two images side-by-side with labels."""
        if img1.size != img2.size:
            logger.error("Cannot stitch images of different sizes.")
            return None
        
        width1, height1 = img1.size
        width2, height2 = img2.size # Should be same as height1

        # Add padding for labels
        label_height = 30 # Adjust as needed
        total_width = width1 + width2
        total_height = height1 + label_height

        stitched_img = Image.new('RGBA', (total_width, total_height), (255, 255, 255, 255)) # White background

        # Paste images
        stitched_img.paste(img1, (0, label_height))
        stitched_img.paste(img2, (width1, label_height))

        # Add labels
        try:
            draw = ImageDraw.Draw(stitched_img)
            # Attempt to load a simple font (adjust path or use default if needed)
            try:
                # On Linux/macOS, common paths
                font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" 
                if not os.path.exists(font_path): font_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf" # macOS fallback
                font = ImageFont.truetype(font_path, 15)
            except IOError:
                logger.warning("Default font not found, using Pillow's default.")
                font = ImageFont.load_default()

            # Label 1 (Baseline)
            label1_pos = (10, 5)
            draw.text(label1_pos, f"1: {label1}", fill=(0, 0, 0, 255), font=font)
            
            # Label 2 (Current)
            label2_pos = (width1 + 10, 5)
            draw.text(label2_pos, f"2: {label2}", fill=(0, 0, 0, 255), font=font)

        except Exception as e:
            logger.warning(f"Could not add labels to stitched image: {e}")
            # Return image without labels if drawing fails

        stitched_img.save("./stitched.png")
        return stitched_img
    
def compare_images(prompt: str, image_bytes_1: bytes, image_bytes_2: bytes, image_client: LLMClient) -> str:
        """
        Compares two images using the multimodal LLM based on the prompt,
        by stitching them into a single image first.
        """
        
        logger.info("Preparing images for stitched comparison...")
        try:
            img1 = Image.open(io.BytesIO(image_bytes_1)).convert("RGBA")
            img2 = Image.open(io.BytesIO(image_bytes_2)).convert("RGBA")

            if img1.size != img2.size:
                 error_msg = f"Visual Comparison Failed: Image dimensions mismatch. Baseline: {img1.size}, Current: {img2.size}."
                 logger.error(error_msg)
                 return f"Error: {error_msg}" # Return error directly

            stitched_image_pil = stitch_images(img1, img2)
            if not stitched_image_pil:
                return "Error: Failed to stitch images."

            # Convert stitched image to bytes
            stitched_buffer = io.BytesIO()
            stitched_image_pil.save(stitched_buffer, format="PNG")
            stitched_image_bytes = stitched_buffer.getvalue()
            logger.info(f"Images stitched successfully (new size: {stitched_image_pil.size}). Requesting LLM comparison...")

        except Exception as e:
             logger.error(f"Error processing images for stitching: {e}", exc_info=True)
             return f"Error: Image processing failed - {e}"


        return image_client.generate_multimodal(prompt, stitched_image_bytes)
    
