class LZString:
    keyStrBase64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    keyStrUriSafe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
    baseReverseDic = {}

    @staticmethod
    def getBaseValue(alphabet, character):
        if alphabet not in LZString.baseReverseDic:
            LZString.baseReverseDic[alphabet] = {c: i for i, c in enumerate(alphabet)}
        return LZString.baseReverseDic[alphabet].get(character)

    @staticmethod
    def decompressFromEncodedURIComponent(input_str):
        if input_str is None:
            return ""
        if input_str == "":
            return None

        input_str = input_str.replace(" ", "+")

        if LZString.keyStrUriSafe not in LZString.baseReverseDic:
            LZString.baseReverseDic[LZString.keyStrUriSafe] = {
                c: i for i, c in enumerate(LZString.keyStrUriSafe)
            }

        reverse_dict = LZString.baseReverseDic[LZString.keyStrUriSafe]

        input_data = [reverse_dict.get(c, 0) for c in input_str]

        return LZString._decompress(len(input_data), 32, input_data)

    @staticmethod
    def _decompress(length, resetValue, input_data):
        dictionary = [0, 1, 2]
        next_val = 0
        enlargeIn = 4
        dictSize = 4
        numBits = 3
        entry = ""
        result = []

        data_val = input_data[0]
        position = resetValue
        index = 1

        bits = 0
        maxpower = 1 << 2
        power = 1
        while power != maxpower:
            resb = data_val & position
            position >>= 1
            if position == 0:
                position = resetValue
                data_val = input_data[index]
                index += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1

        next_val = bits
        if next_val == 0:
            bits = 0
            maxpower = 1 << 8
            power = 1
            while power != maxpower:
                resb = data_val & position
                position >>= 1
                if position == 0:
                    position = resetValue
                    data_val = input_data[index]
                    index += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1
            c = chr(bits)
        elif next_val == 1:
            bits = 0
            maxpower = 1 << 16
            power = 1
            while power != maxpower:
                resb = data_val & position
                position >>= 1
                if position == 0:
                    position = resetValue
                    data_val = input_data[index]
                    index += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1
            c = chr(bits)
        elif next_val == 2:
            return ""

        dictionary.append(c)
        w = c
        result.append(c)

        while True:
            if index > length:
                return ""

            bits = 0
            maxpower = 1 << numBits
            power = 1
            while power != maxpower:
                resb = data_val & position
                position >>= 1
                if position == 0:
                    position = resetValue
                    data_val = input_data[index]
                    index += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1

            c = bits
            if c == 0:
                bits = 0
                maxpower = 1 << 8
                power = 1
                while power != maxpower:
                    resb = data_val & position
                    position >>= 1
                    if position == 0:
                        position = resetValue
                        data_val = input_data[index]
                        index += 1
                    bits |= (1 if resb > 0 else 0) * power
                    power <<= 1

                dictionary.append(chr(bits))
                c = dictSize
                dictSize += 1
                enlargeIn -= 1
            elif c == 1:
                bits = 0
                maxpower = 1 << 16
                power = 1
                while power != maxpower:
                    resb = data_val & position
                    position >>= 1
                    if position == 0:
                        position = resetValue
                        data_val = input_data[index]
                        index += 1
                    bits |= (1 if resb > 0 else 0) * power
                    power <<= 1
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

            result.append(entry)

            dictionary.append(w + entry[0])
            dictSize += 1
            enlargeIn -= 1

            w = entry

            if enlargeIn == 0:
                enlargeIn = 1 << numBits
                numBits += 1


decompressFromEncodedURIComponent = LZString.decompressFromEncodedURIComponent
