"""Optional OpenCV face tracking for smooth vertical reframing."""

def _crop_expression(points, crop_width):
    if not points:
        return None
    expr = f"{points[-1][1]:.2f}"
    for i in range(len(points) - 2, -1, -1):
        f1, x1 = points[i]
        f2, x2 = points[i + 1]
        if f2 <= f1:
            continue
        linear = f"{x1:.2f}+({x2 - x1:.2f})*(n-{f1})/{f2 - f1}"
        expr = f"if(lt(n\\,{f2})\\,{linear}\\,{expr})"
    first_frame, first_x = points[0]
    if first_frame > 0:
        expr = f"if(lt(n\\,{first_frame})\\,{first_x:.2f}\\,{expr})"
    # Keep every value within the horizontal frame bounds.
    return f"max(0\\,min(iw-{crop_width}\\,{expr}))"


def build_dynamic_crop(video_path, start, end, target_ratio=9 / 16, sample_fps=3, alpha=0.35):
    """Return an FFmpeg crop filter string, or None to use the center-crop fallback."""
    capture = None
    try:
        import cv2

        capture = cv2.VideoCapture(video_path)
        if not capture.isOpened():
            return None
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width < 2 or height < 2:
            return None

        crop_width = min(width, max(2, int(height * target_ratio)))
        if crop_width >= width:
            return None
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        detector = cv2.CascadeClassifier(cascade_path)
        if detector.empty():
            return None

        start_frame = max(0, int(start * fps))
        end_frame = max(start_frame + 1, int(end * fps))
        stride = max(1, int(round(fps / max(1, sample_fps))))
        capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        points = []
        previous_face_x = None
        smoothed_center = None
        crop_half = crop_width / 2.0
        max_center = width - crop_half

        for source_frame in range(start_frame, end_frame, stride):
            capture.set(cv2.CAP_PROP_POS_FRAMES, source_frame)
            ok, frame = capture.read()
            if not ok:
                continue
            scale = min(1.0, 640.0 / width)
            small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1.0 else frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5,
                minSize=(max(24, int(36 * scale)), max(24, int(36 * scale)))
            )
            if len(faces) == 0:
                continue

            candidates = []
            for x, y, face_w, face_h in faces:
                center_x = (x + face_w / 2.0) / scale
                area = face_w * face_h
                distance = abs(center_x - previous_face_x) if previous_face_x is not None else 0
                candidates.append((center_x, area, distance))
            if previous_face_x is None:
                face_x = max(candidates, key=lambda item: item[1])[0]
            else:
                face_x = min(candidates, key=lambda item: item[2] - item[1] * 0.0001)[0]
            previous_face_x = face_x

            target_center = min(max(face_x, crop_half), max_center)
            smoothed_center = target_center if smoothed_center is None else (
                alpha * target_center + (1.0 - alpha) * smoothed_center
            )
            smoothed_center = min(max(smoothed_center, crop_half), max_center)
            crop_x = min(max(smoothed_center - crop_half, 0.0), width - crop_width)
            points.append((source_frame - start_frame, crop_x))

        if not points:
            return None
        expr = _crop_expression(points, crop_width)
        if not expr:
            return None
        return f"crop=w={crop_width}:h={height}:x='{expr}':y=0"
    except Exception as exc:
        print(f"[Face Tracker] Detection unavailable ({exc}); using center crop.")
        return None
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
