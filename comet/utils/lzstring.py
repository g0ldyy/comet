_MAX_COMPRESSED_CHARACTERS = 8 * 1024 * 1024
_MAX_DECOMPRESSED_CHARACTERS = 64 * 1024 * 1024
_URI_SAFE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
_URI_SAFE_REVERSE = {
    character: index for index, character in enumerate(_URI_SAFE_ALPHABET)
}


class LZString:
    @staticmethod
    def decompressFromEncodedURIComponent(
        input_str,
        *,
        maximum: int = _MAX_DECOMPRESSED_CHARACTERS,
    ):
        if input_str is None:
            return ""
        if input_str == "":
            return None
        if type(input_str) is not str or len(input_str) > _MAX_COMPRESSED_CHARACTERS:
            return None
        maximum = min(maximum, _MAX_DECOMPRESSED_CHARACTERS)

        input_str = input_str.replace(" ", "+")

        input_data = [_URI_SAFE_REVERSE.get(c) for c in input_str]
        if any(value is None for value in input_data):
            return None

        return LZString._decompress(
            len(input_data),
            32,
            input_data,
            maximum=maximum,
        )

    @staticmethod
    def _decompress(length, resetValue, input_data, *, maximum):
        dictionary = [0, 1, 2]
        enlargeIn = 4
        dictSize = 4
        numBits = 3
        result = []
        result_length = 0

        data_val = input_data[0]
        position = resetValue
        index = 1

        def read_bits(bit_count):
            nonlocal data_val, index, position
            bits = 0
            power = 1
            maxpower = 1 << bit_count
            while power != maxpower:
                resb = data_val & position
                position >>= 1
                if position == 0:
                    position = resetValue
                    if index >= length:
                        return None
                    data_val = input_data[index]
                    index += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1
            return bits

        next_val = read_bits(2)
        if next_val == 0:
            bits = read_bits(8)
            if bits is None:
                return None
            c = chr(bits)
        elif next_val == 1:
            bits = read_bits(16)
            if bits is None:
                return None
            c = chr(bits)
        elif next_val == 2:
            return ""
        else:
            return None

        dictionary.append(c)
        w = c
        result.append(c)
        result_length = 1

        while True:
            c = read_bits(numBits)
            if c is None:
                return None
            if c == 0:
                bits = read_bits(8)
                if bits is None:
                    return None
                dictionary.append(chr(bits))
                c = dictSize
                dictSize += 1
                enlargeIn -= 1
            elif c == 1:
                bits = read_bits(16)
                if bits is None:
                    return None
                dictionary.append(chr(bits))
                c = dictSize
                dictSize += 1
                enlargeIn -= 1
            elif c == 2:
                return "".join(result)

            if enlargeIn == 0:
                enlargeIn = 1 << numBits
                numBits += 1

            if c < len(dictionary):
                entry = dictionary[c]
            elif c == dictSize:
                entry = w + w[0]
            else:
                return None

            if len(entry) > maximum - result_length:
                return None
            result.append(entry)
            result_length += len(entry)

            dictionary.append(w + entry[0])
            dictSize += 1
            enlargeIn -= 1

            w = entry

            if enlargeIn == 0:
                enlargeIn = 1 << numBits
                numBits += 1


decompressFromEncodedURIComponent = LZString.decompressFromEncodedURIComponent
