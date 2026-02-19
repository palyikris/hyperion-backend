import enum


class MediaStatus(enum.Enum):
    PENDING = "PENDING"  
    UPLOADED = "UPLOADED"  
    EXTRACTING = "EXTRACTING"  
    PROCESSING = "PROCESSING" 
    READY = "READY"  
    FAILED = "FAILED"
