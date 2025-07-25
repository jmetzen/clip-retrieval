"""Reader module provides files and webdataset readers"""

import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from torch.utils.data import DataLoader
from torch.utils.data.dataloader import default_collate


def folder_to_keys(folder, enable_text=True, enable_image=True, enable_metadata=False):
    """returns a list of keys from a folder of images and text"""
    path = Path(folder)
    text_files = None
    metadata_files = None
    image_files = None
    if enable_text:
        text_files = [*path.glob("**/*.txt")]
        text_files = {
            text_file.relative_to(path).as_posix(): text_file
            for text_file in text_files
        }
    if enable_image:
        image_files = [
            *path.glob("**/*.png"),
            *path.glob("**/*.jpg"),
            *path.glob("**/*.jpeg"),
            *path.glob("**/*.bmp"),
            *path.glob("**/*.webp"),
            *path.glob("**/*.PNG"),
            *path.glob("**/*.JPG"),
            *path.glob("**/*.JPEG"),
            *path.glob("**/*.BMP"),
            *path.glob("**/*.WEBP"),
        ]
        image_files = {
            image_file.relative_to(path).as_posix(): image_file
            for image_file in image_files
        }
    if enable_metadata:
        metadata_files = [*path.glob("**/*.json")]
        metadata_files = {
            metadata_file.relative_to(path).as_posix(): metadata_file
            for metadata_file in metadata_files
        }

    keys = None

    def join(new_set):
        return new_set & keys if keys is not None else new_set

    if enable_text:
        keys = join(text_files.keys())
    elif enable_image:
        keys = join(image_files.keys())
    elif enable_metadata:
        keys = join(metadata_files.keys())
    keys = list(sorted(keys))
    return keys, text_files, image_files, metadata_files


def get_image_dataset():
    """retrieve image dataset module without importing torch at the top level"""

    from torch.utils.data import \
        Dataset  # pylint: disable=import-outside-toplevel

    class ImageDataset(Dataset):
        """ImageDataset is a pytorch Dataset exposing image and text tensors from a folder of image and text"""

        def __init__(
            self,
            preprocess,
            tokenizer,
            folder,
            enable_text=True,
            enable_image=True,
            enable_metadata=False,
            input_sampler=lambda a: a,
        ):
            super().__init__()

            self.keys, text_files, image_files, metadata_files = folder_to_keys(
                folder, enable_text, enable_image, enable_metadata
            )
            self.keys = input_sampler(self.keys)
            self.enable_text = enable_text
            self.enable_image = enable_image
            self.enable_metadata = enable_metadata
            keys_set = set(self.keys)
            if self.enable_text:
                self.tokenizer = lambda text: tokenizer([text])[0]
                self.text_files = {k: v for k, v in text_files.items() if k in keys_set}
            if self.enable_image:
                self.image_files = {
                    k: v for k, v in image_files.items() if k in keys_set
                }
                self.image_transform = preprocess
            if self.enable_metadata:
                self.metadata_files = {
                    k: v for k, v in metadata_files.items() if k in keys_set
                }

        def __len__(self):
            return len(self.keys)

        def __getitem__(self, ind):
            key = self.keys[ind]
            output = {}

            if self.enable_image:
                image_file = self.image_files[key]
                try:
                    image_tensor = self.image_transform(Image.open(image_file))
                except (UnidentifiedImageError, OSError) as e:
                    print(f"Failed to load image {image_file}. Error: {e}. Skipping.")
                    return None  # return None to be filtered in the batch collate_fn
                output["image_filename"] = str(image_file)
                output["image_tensor"] = image_tensor

            if self.enable_text:
                text_file = self.text_files[key]
                caption = text_file.read_text()
                tokenized_text = self.tokenizer(caption)
                output["text_tokens"] = tokenized_text
                output["text"] = caption

            if self.enable_metadata:
                metadata_file = self.metadata_files[key]
                metadata = metadata_file.read_text()
                output["metadata"] = metadata

            return output

    return ImageDataset


def create_webdataset(
    urls,
    image_transform,
    tokenizer,
    enable_text=True,
    enable_image=True,
    image_key="jpg",
    caption_key="txt",
    enable_metadata=False,
    cache_path=None,
    input_sampler=lambda a: a,
):
    """Create a WebDataset reader, it can read a webdataset of image, text and json"""
    import webdataset as wds  # pylint: disable=import-outside-toplevel

    urls = input_sampler(urls)

    dataset = wds.WebDataset(
        urls,
        cache_dir=cache_path,
        cache_size=10**10,
        handler=wds.handlers.warn_and_continue,
    )

    def _tokenizer(text):
        return tokenizer([text])[0]

    def filter_dataset(item):
        if enable_text and caption_key not in item:
            return False
        if enable_image and image_key not in item:
            return False
        if enable_metadata and "json" not in item:
            return False
        return True

    filtered_dataset = dataset.select(filter_dataset)

    def preprocess_dataset(item):
        output = {}
        if enable_image:
            image_data = item[image_key]
            image = Image.open(io.BytesIO(image_data))
            image_tensor = image_transform(image)
            output["image_filename"] = item["__key__"]
            output["image_tensor"] = image_tensor

        if enable_text:
            text = item[caption_key]
            caption = text.decode("utf-8")
            tokenized_text = _tokenizer(caption)
            output["text_tokens"] = tokenized_text
            output["text"] = caption

        if enable_metadata:
            metadata_file = item["json"]
            metadata = metadata_file.decode("utf-8")
            output["metadata"] = metadata
        return output

    transformed_dataset = filtered_dataset.map(
        preprocess_dataset, handler=wds.handlers.warn_and_continue
    )
    return transformed_dataset


def dataset_to_dataloader(dataset, batch_size, num_prepro_workers, input_format):
    """Create a pytorch dataloader from a dataset"""

    def collate_fn(batch):
        batch = list(filter(lambda x: x is not None, batch))
        return default_collate(batch)

    data = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_prepro_workers,
        pin_memory=True,
        # prefetch_factor=2,
        collate_fn=collate_fn if input_format == "files" else None,
    )
    return data


class FilesReader:
    """FilesReader is a reader that reads files from a folder"""

    def __init__(
        self,
        sampler,
        preprocess,
        tokenizer,
        input_dataset,
        batch_size,
        num_prepro_workers,
        enable_text=True,
        enable_image=True,
        enable_metadata=False,
    ) -> None:
        super().__init__()
        dataset = get_image_dataset()(
            preprocess,
            tokenizer,
            input_dataset,
            enable_text,
            enable_image,
            enable_metadata,
            sampler,
        )
        self.dataloader = dataset_to_dataloader(
            dataset, batch_size, num_prepro_workers, "files"
        )

    def __iter__(self):
        for batch in self.dataloader:
            yield batch


class WebdatasetReader:
    """WebdatasetReader is a reader that reads samples from a webdataset"""

    def __init__(
        self,
        sampler,
        preprocess,
        tokenizer,
        input_dataset,
        batch_size,
        num_prepro_workers,
        enable_text=True,
        enable_image=True,
        enable_metadata=False,
        wds_image_key="jpg",
        wds_caption_key="txt",
        cache_path=None,
    ):
        self.batch_size = batch_size
        dataset = create_webdataset(
            input_dataset,
            preprocess,
            tokenizer,
            enable_text=enable_text,
            enable_image=enable_image,
            image_key=wds_image_key,
            caption_key=wds_caption_key,
            enable_metadata=enable_metadata,
            cache_path=cache_path,
            input_sampler=sampler,
        )
        self.dataloader = dataset_to_dataloader(
            dataset, batch_size, num_prepro_workers, "webdataset"
        )

    def __iter__(self):
        for batch in self.dataloader:
            yield batch
